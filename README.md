# OmniVoice Agent

Plataforma de agentes de voz en tiempo real: streaming bidireccional, barge-in, herramientas con datos reales y observabilidad.

## Arranque rápido

```bash
cp .env.example .env        # añade OPENAI_API_KEY (sin clave se usa un proveedor simulado)
docker compose up --build
```

- Conversación: http://localhost:3000/conversation
- Panel: http://localhost:3000/dashboard
- API: http://localhost:8000/docs · Métricas: http://localhost:8000/metrics

Tests del backend: `cd backend && pip install -r requirements.txt && pytest -q`

## Arquitectura

```
Navegador (AudioWorklet PCM16 + VAD local) ──WS──> FastAPI /ws/audio
                                                      │
                          VoiceSession (asyncio + máquina de estados)
                          ├─ VAD servidor ─ barge-in ─ cancel + audio.clear
                          ├─ RealtimeProvider (OpenAI Realtime | Fake)
                          └─ ToolRegistry (Pydantic + permisos + timeout + idempotencia)
                                                      │
                                          PostgreSQL (RLS por org) · Redis · Prometheus
```

### Decisiones clave

- **Barge-in en dos niveles**: el cliente corta su buffer de reproducción en cuanto su VAD local detecta voz (sin esperar a la red) y avisa al servidor, que cancela la respuesta del proveedor y descarta audio obsoleto con un contador de época. Se mide el tiempo hasta el silencio efectivo.
- **El modelo no accede a la base de datos**: solo propone llamadas; el backend valida argumentos (`extra="forbid"`), comprueba permisos, filtra por `org_id` y devuelve resultados reales. Las escrituras son idempotentes por `call_id`.
- **Sin claves en el navegador**: el cliente recibe un ticket WS de un solo uso y 30 s de vida.
- **Métricas**: TTFB (primer audio), tiempo de silencio tras interrupción, paquetes perdidos por huecos de secuencia, duración de herramientas, errores, sesiones activas.

## Estado y limitaciones honestas

- Las metas (p50 < 500 ms, barge-in p95 < 200 ms) son **objetivos a validar con pruebas de carga**; dependen del proveedor y la red.
- `InMemoryRepository` es el repositorio por defecto; `migrations/001_init.sql` define el esquema PostgreSQL con RLS, pero el repositorio PostgreSQL y la persistencia de `audit_logs` están pendientes de implementar.
- Los nombres de eventos del adaptador OpenAI Realtime deben verificarse contra la versión vigente de la API.
- Pendiente: Redis para estado compartido entre réplicas, rate limiting, telefonía, múltiples agentes, panel con roles reales y pruebas de carga.
