# OmniVoice Agent

Plataforma de agentes de voz en tiempo real: streaming bidireccional por WebSocket, interrupciones (barge-in) con corte local inmediato, herramientas con datos reales, multi-tenant con aislamiento a nivel de base de datos y observabilidad.

## Arranque rápido

```bash
cp .env.example .env        # añade OPENAI_API_KEY; sin clave se usa un proveedor simulado
docker compose up --build   # Postgres (migraciones 001-003), Redis, backend, frontend, Prometheus, Grafana
```

| Qué | Dónde |
|---|---|
| Conversación | http://localhost:3000/conversation |
| Panel, agentes, historial, configuración | `/dashboard`, `/agents`, `/history`, `/settings` |
| API + OpenAPI | http://localhost:8000/docs |
| Métricas Prometheus / Grafana | `:8000/metrics` · `:9090` · `:3001` |

**Sin Docker:** `cd backend && pip install -r requirements.txt && PERSISTENCE_BACKEND=memory STATE_BACKEND=memory uvicorn app.main:app` y `cd frontend && npm install && npm run dev`.

## Pruebas

```bash
cd backend && pytest -q                                   # unitarias, servicios, API y WebSocket
python -m bench.orchestrator_bench --sessions 200         # overhead del orquestador (sin red)
python -m bench.loadtest --url http://localhost:8000 --clients 50 --duration 20   # extremo a extremo
cd ../frontend && npm run typecheck && npm run build
```

CI (`.github/workflows/ci.yml`) ejecuta pytest, un benchmark de humo, aplica las migraciones sobre un PostgreSQL real y comprueba que **RLS aísla organizaciones** y que `omni_app` no es superusuario, y compila el frontend.

## Qué incluye

- **Voz en tiempo real:** PCM16 24 kHz, frames de 20 ms con número de secuencia (detecta paquetes perdidos), AudioWorklet de captura y reproducción, adaptador OpenAI Realtime y proveedor simulado.
- **Barge-in en dos niveles:** el navegador vacía su buffer al detectar voz; el servidor cancela la respuesta, descarta audio y resultados de herramientas obsoletos y mide el tiempo hasta el silencio.
- **Herramientas seguras:** `check_inventory`, `check_reservation`, `create_reservation`, `create_support_ticket`, `search_knowledge_base`, `transfer_to_human`. Validación estricta, permisos por rol, lista de herramientas por agente, timeout, idempotencia en escrituras y aislamiento por organización.
- **Seguridad:** JWT con roles, tickets WS de un solo uso (30 s), RLS forzada en PostgreSQL con rol sin superusuario, auditoría append-only, rate limiting, cupo de sesiones por organización, límites de tamaño de frame / inactividad / duración, cabeceras de seguridad, arranque bloqueado si la configuración de producción es insegura.
- **Observabilidad:** logs JSON con `request_id` y `correlation_id` de sesión, métricas Prometheus (primer audio, silencio tras interrupción, paquetes perdidos, herramientas, errores, sesiones), percentiles p50/p95/p99 en `/api/v1/metrics`, `/healthz` y `/readyz`.
- **Datos:** 16 tablas con migraciones, conversaciones, transcripciones, eventos, ejecuciones de herramientas, consumo y coste estimado.

Documentación: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) (flujo, ADRs, límites) · [`docs/RUNBOOK.md`](docs/RUNBOOK.md) (despliegue, alertas, incidentes).

## Estado y limitaciones honestas

- **Latencia:** p50 < 500 ms y barge-in p95 < 200 ms son **objetivos a validar** con `bench/loadtest.py` contra tu infraestructura y proveedor. Medido aquí solo el orquestador aislado: el manejo de un barge-in en el servidor tarda del orden de microsegundos con 200 sesiones simultáneas; la latencia real depende del proveedor y la red.
- **Pendiente de verificar en tu entorno:** los nombres de eventos del adaptador OpenAI Realtime deben contrastarse con la versión vigente de la API (no se probó contra el servicio real).
- **No implementado:** purga automática por `retention_days` (ver runbook), SSO/gestión de usuarios (el JWT lo emite tu proveedor de identidad), telefonía, grabación de audio, transferencia efectiva a un operador humano (la herramienta solo encola la solicitud), selector de voz/idioma en la UI.
- El VAD es por energía; en entornos ruidosos conviene un VAD basado en modelo.
- Percentiles de `/api/v1/metrics` por proceso; con varias réplicas usa los histogramas de Prometheus.
