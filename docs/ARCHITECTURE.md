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

## Límites conocidos
- Los percentiles de `/api/v1/metrics` son por proceso; en varias réplicas usar Prometheus (histogramas) y agregar.
- El rate limiter es de ventana fija (permite ráfagas de hasta 2× el límite en el cambio de ventana).
- El VAD por energía no distingue voz de ruido fuerte; para entornos ruidosos sustituir por Silero VAD o WebRTC VAD.
- No hay SSO ni gestión de usuarios: el JWT lo emite un proveedor de identidad externo; `/auth/dev-token` solo existe
  fuera de producción.
- El audio no se persiste. Si se necesita grabación hay que añadir almacenamiento de objetos con consentimiento y retención.
