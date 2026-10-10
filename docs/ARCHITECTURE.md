# Arquitectura y decisiones (ADR)

## Flujo de una conversación

```
1. POST /api/v1/sessions            (JWT)  -> session_id        [rate limit + cupo por org]
2. POST /api/v1/sessions/{id}/connect (JWT) -> ticket WS 30 s, un solo uso
3. WS  /ws/audio?ticket=...                 -> PCM16 24 kHz mono, frames de 20 ms:  [seq u32 BE][pcm]
        cliente -> servidor: binario (audio) | texto {"type":"barge_in"|"end"}
        servidor -> cliente: binario (audio del agente) | texto: session.ready, audio.clear,
                              transcript_user/agent, tool.start/end, metrics, state, error
```

Componentes: `VoiceSession` (orquestador + máquina de estados) · `RealtimeProvider` (OpenAI Realtime | Fake) ·
`ToolRegistry` (validación, permisos, timeout, idempotencia) · `Persistence` (Postgres | memoria) ·
`StateStore` (Redis | memoria) · métricas Prometheus + ventanas p50/p95/p99.

## ADR-1: Barge-in en dos niveles
**Decisión.** El cliente corta su propio buffer de reproducción en cuanto su VAD local detecta voz y avisa por WS;
el servidor cancela la respuesta del proveedor, incrementa una *época* y descarta audio y resultados de herramientas
obsoletos. **Por qué.** Cancelar en el servidor no vacía el audio ya almacenado en el navegador; esperar a la red
añadiría decenas o cientos de ms. **Coste.** Dos VAD que calibrar; el del navegador usa umbral de energía y puede dar
falsos positivos con ruido (mitigado con `echoCancellation`, `minSpeechMs` y la confirmación del servidor).

## ADR-2: El modelo propone, el backend dispone
**Decisión.** El modelo nunca accede a datos. Cada llamada pasa por: herramienta permitida para el agente → permiso del
rol → validación Pydantic (`extra="forbid"`) → timeout → ejecución filtrada por `org_id` → resultado real al modelo.
Las escrituras son idempotentes por `(org, call_id, tool)`. En BD no se guardan los argumentos, solo sus claves.
**Por qué.** Evita que una alucinación o inyección de prompt escriba datos o lea de otra organización.

## ADR-3: Aislamiento multi-tenant con RLS
**Decisión.** Toda operación abre una transacción con `set_config('app.org_id', ..., true)` y las tablas tienen políticas
RLS `FORCE`. La app se conecta con `omni_app` (sin superusuario, `NOBYPASSRLS`). `audit_logs` es append-only
(`REVOKE UPDATE, DELETE`). **Por qué.** Un `WHERE org_id` olvidado no filtra datos. **Coste.** `org_id` debe ser UUID
y cada consulta paga un `set_config`.

## ADR-4: Tickets WS de un solo uso
**Decisión.** El JWT nunca va en la URL del WebSocket; se canjea por un ticket de 30 s consumido de forma atómica
(`GETDEL` en Redis). El ticket del paso 2 queda ligado al usuario que creó la sesión.

## ADR-5: Estado compartido en Redis, no en el proceso
Tickets, rate limiting (ventana fija) y cupo de sesiones (Lua atómico + expiración de sesiones huérfanas) viven en
Redis para poder escalar a varias réplicas. Redis no guarda nada que no pueda perderse.

## ADR-6: Persistir nunca rompe la llamada
Los fallos de escritura se registran y se cuentan (`omnivoice_errors_total{kind="persistence"}`) pero no cortan la
conversación. **Trade-off asumido:** ante una caída prolongada de PostgreSQL se pierde auditoría de esas sesiones.
Si el requisito legal fuese "sin auditoría no hay llamada", cambiar `_safe` por fallo duro en `pg_persistence.py`.

## ADR-7: Proveedor en cascada además de voz-a-voz
**Decisión.** Además de los proveedores voz-a-voz (OpenAI Realtime, Gemini Live) existe `CascadedProvider`: STT en streaming (Deepgram) →
LLM en streaming con herramientas (Anthropic) → TTS en streaming (Deepgram), tras el mismo contrato `RealtimeProvider`.
**Por qué.** Un servicio voz-a-voz no permite medir ni optimizar por separado reconocimiento, inferencia y síntesis; la cascada sí
(eventos `stage` → `omnivoice_stage_seconds{stage}`), y permite cambiar cada pieza. **Coste.** Más latencia base (el endpointing del STT
suma ~300 ms) y tres servicios que pueden fallar. **Detalles clave:** el LLM y el TTS se solapan por frases (el TTS empieza con la primera frase);
el barge-in cancela las tareas (se cierra el stream HTTP del LLM, no solo se ignora su salida); un turno interrumpido solo conserva en el historial
lo que se llegó a decir, nunca un `tool_use` sin su `tool_result`.

## ADR-8: Recuperación de fallos del proveedor, acotada
**Decisión.** Si el proveedor cae (error, cierre del flujo, fallo al enviar), la sesión crea uno nuevo con la fábrica del servidor, le pasa
los últimos 6 intercambios (solo en memoria) como contexto, avisa al cliente (`provider.reconnecting` / `provider.reconnected`) y descarta el turno en curso
(época nueva + `audio.clear`). Tope: `PROVIDER_MAX_RECONNECTS` por sesión con espera exponencial; agotado, `provider_unavailable` y estado `ERROR`.
Toda conexión tiene plazo (`PROVIDER_CONNECT_TIMEOUT_S`). **Por qué.** Una caída de red de un segundo no debe costar la llamada, pero reintentar sin
límite convierte un incidente del proveedor en consumo y sesiones zombi. **Invariantes verificadas por tests:** sin tareas huérfanas tras cerrar (también
cerrando a mitad de una reconexión), un proveedor que termina de conectar después del cierre se cierra, y los eventos de un proveedor sustituido se ignoran.
En el STT de Deepgram, la reconexión es interna (el audio durante el corte se descarta, no se acumula).

## ADR-9: Medir el fin de turno desde la última voz, no desde el fin declarado por el VAD
El VAD declara «fin de habla» tras 600 ms de silencio (hangover). Medir desde ahí ocultaba esos 600 ms y perdía muestras cuando el proveedor respondía antes.
Ahora el fin de turno es el último chunk con voz (ver `docs/LATENCY.md` §1). Efecto: las cifras publicadas antes de este cambio no son comparables.

## Límites conocidos
- Los percentiles de `/api/v1/metrics` son por proceso; en varias réplicas usar Prometheus (histogramas) y agregar.
- El rate limiter es de ventana fija (permite ráfagas de hasta 2× el límite en el cambio de ventana).
- El VAD por energía no distingue voz de ruido fuerte; para entornos ruidosos sustituir por Silero VAD o WebRTC VAD.
- No hay SSO ni gestión de usuarios: el JWT lo emite un proveedor de identidad externo; `/auth/dev-token` solo existe
  fuera de producción.
- El audio no se persiste salvo grabación con consentimiento (ver README).
- El presupuesto diario por organización se carga al cerrar la sesión (ver `docs/COSTS.md`).
