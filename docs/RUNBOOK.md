# Runbook

## Despliegue
1. Con el overlay de producción las migraciones y la clave de `omni_app` se aplican solas (ver README). Manual: aplica `migrations/*.sql` con un administrador y cambia la clave de `omni_app`.
   en `002_app_role.sql` (o créala fuera del script) y **no apliques el seed `003`** (datos demo).
2. Variables obligatorias en producción: `ENVIRONMENT=production`, `PERSISTENCE_BACKEND=postgres`,
   `STATE_BACKEND=redis`, `JWT_SECRET` (≥ 32 caracteres), `CORS_ORIGINS` explícito, `OPENAI_API_KEY`.
   El proceso **se niega a arrancar** si alguna es insegura.
3. Colocar un proxy TLS delante (WSS). Timeout de WS ≥ 65 s (el servidor cierra a los 60 s de inactividad).
4. Probes: liveness `GET /healthz`, readiness `GET /readyz` (comprueba BD y Redis).

## Alertas sugeridas (Prometheus)
| Señal | Expresión | Umbral |
|---|---|---|
| Latencia p50 al primer audio | `histogram_quantile(0.5, rate(omnivoice_first_audio_seconds_bucket[5m]))` | > 0.5 s durante 10 min |
| Silencio tras interrupción p95 | `histogram_quantile(0.95, rate(omnivoice_barge_in_seconds_bucket[5m]))` | > 0.2 s |
| Errores | `rate(omnivoice_errors_total[5m])` | > 1/s |
| Fallos de persistencia | `rate(omnivoice_errors_total{kind="persistence"}[5m])` | > 0 durante 5 min |
| Saturación | `omnivoice_active_sessions` | > 80 % de `MAX_SESSIONS_PER_ORG × orgs` |

## Incidentes
- **El agente no responde / `provider_unavailable`**: revisar clave y cuota del proveedor y `omnivoice_errors_total{kind="provider_loop"}`.
  Las sesiones afectadas pasan a `ERROR` y el cliente recibe `error`.
- **Usuarios reciben 429**: cupo de la organización (`MAX_SESSIONS_PER_ORG`) o rate limit. Un cupo "fantasma" se libera
  solo a las 2 h; para forzarlo, borrar la clave `sessions:<org_id>` en Redis.
- **No aparecen conversaciones en el panel**: comprobar que `org_id` del JWT es un UUID (con Postgres) y que
  `omni_app` no es superusuario (`SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname='omni_app'` debe dar `f,f`).
- **Investigar una sesión**: buscar su `correlation_id` en `audit_logs.detail` y en los logs JSON.

## Fallos de proveedor, límites y costes
- **Reconexiones:** `omnivoice_provider_reconnects_total{outcome}` (`ok`/`failed`). Muchas `failed` seguidas = proveedor caído o clave/cuota: las sesiones
  terminan con `provider_unavailable` tras `PROVIDER_MAX_RECONNECTS`. Alerta sugerida: `rate(omnivoice_provider_reconnects_total{outcome="failed"}[5m]) > 0`.
- **429 / cierre 4429 inesperados:** `omnivoice_budget_rejected_total{reason}`: `global_concurrency` (sube `MAX_TOTAL_SESSIONS` o añade réplicas) u
  `org_daily_budget` (consumo diario agotado; el contador es `usage:<org>:<YYYYMMDD>` en Redis y caduca solo; para levantar el tope antes, borra esa clave).
- **Latencia por etapa:** `histogram_quantile(0.95, rate(omnivoice_stage_seconds_bucket{stage="llm"}[5m]))` (y `stt`, `tts`) localiza qué servicio degrada el primer audio (solo cascada).
- **Coste/min:** ver `docs/COSTS.md`. Compara con tu factura real: los precios de `PRICE_*` son de referencia.
- **Logs:** no contienen audio, transcripciones ni argumentos de herramientas; las credenciales se redactan (`redact_secrets`) incluso dentro de URLs y excepciones.

## Retención y borrado
Un job (activo por defecto, `RETENTION_JOB_ENABLED`) purga cada `RETENTION_INTERVAL_SECONDS` lo anterior a
`organizations.retention_days`. Borra primero los objetos de grabación y después las filas; un bloqueo en Redis evita
que dos réplicas lo ejecuten a la vez. La auditoría es solo-anexar y no se purga. Cada pasada deja un registro de auditoría.
- Base existente: aplicar las migraciones pendientes con el ejecutor (ver «Migraciones»).
- Activar grabación en una organización: `UPDATE organizations SET recording_enabled = true WHERE id = ...` y configurar `RECORDING_STORAGE=s3`.

## Telefonía
- Configurar en Twilio: Voice webhook `POST {TELEPHONY_PUBLIC_URL}/telephony/voice` del número.
- Si las llamadas entran y se cortan: revisar firma (la URL pública debe coincidir exactamente con la que Twilio llama),
  `ERRORS{kind="telephony_bad_signature"}` y que el WS `/ws/telephony` sea alcanzable por wss.
- Transferencia sin efecto: comprobar `HUMAN_TRANSFER_NUMBER` y la métrica `telephony_transfer`.

## Pruebas de rendimiento
- Orquestador aislado (sin red): `python -m bench.orchestrator_bench --sessions 200`.
- Extremo a extremo: levantar el stack y `python -m bench.loadtest --url http://localhost:8010 --clients 50 --duration 20`.
  Con el proveedor simulado mide tu stack; con `OPENAI_API_KEY` mide también al proveedor y la red.
  Sube `--clients` hasta ver 429 o aumento de p95 para fijar la capacidad por réplica.

Metodología completa, objetivos y resultados: `docs/LATENCY.md` (`bash bench/run_matrix.sh` los reproduce sin claves).
Informe publicable: `python -m bench.loadtest --url ... --clients 50 --duration 30 --mode real --label "..." --out r.json`
y `python -m bench.report r.json > docs/LATENCY.md`. El loadtest usa habla sintética: ejecútalo con `VAD_BACKEND=energy`.
Contratos con los proveedores: `OPENAI_API_KEY=... GEMINI_API_KEY=... pytest -m contract tests/contract` (cada prueba se omite si falta su clave).

## Migraciones
Con versión, desde `backend/` y con el rol de migraciones (nunca `omni_app`):
```
export ADMIN_DATABASE_URL=postgresql+asyncpg://omni:<clave>@<host>:5432/omnivoice
python -m app.migrate status      # applied / pending por archivo
python -m app.migrate up          # aplica las pendientes (cada archivo en su transacción)
```
- Una migración aplicada **no se edita** (el checksum lo detecta y `up` se detiene): los cambios van en un archivo nuevo `NNN_descripcion.sql`.
- Bases anteriores a este mecanismo (creadas por `initdb` o a mano) no se migran solas. Indica hasta qué versión ya tenían:
  `python -m app.migrate baseline --through 004` y luego `up`.
- Si `up` falla, el archivo se revierte y no queda registrado; corrige y repite. Dos ejecuciones simultáneas se rechazan.
- `docker-compose` sigue aplicando `migrations/*.sql` con `initdb` solo en el primer arranque de un volumen vacío; esa base queda sin registro
  y requiere `baseline --through <última>` antes del primer `up` posterior.

## Enrutado de llamadas por número
Cada llamada se asigna por el número marcado (`To`; en salientes, `From`) usando `phone_numbers`: ese número fija la organización y el agente.
- Solo contesta un agente **publicado** (`POST /api/v1/agents/{id}/publish`). Borrador, archivado o número sin agente → mensaje y colgado, con `telephony.call_rejected` en auditoría.
- Número sin asignar: por defecto usa `TELEPHONY_ORG_ID`/`TELEPHONY_AGENT_ID` (compatibilidad). En producción multi-cliente pon `TELEPHONY_ALLOW_UNMAPPED=false` para rechazarlos.
- La búsqueda usa la función `resolve_phone_number` (migración 006, `SECURITY DEFINER`): su dueño debe ser el rol de migraciones, que no está sujeto a RLS (en la imagen oficial de PostgreSQL lo es). No lista números: solo resuelve uno exacto.

## Estados de llamada
Cada llamada es una fila en `calls` (única por proveedor + `CallSid`) con su historial en `call_events`. Estados: CREATED, QUEUED, RINGING, ANSWERED, CONNECTED, COMPLETED, FAILED, BUSY, NO_ANSWER, CANCELLED, TRANSFERRED.
- El estado solo avanza; un evento tardío o repetido se guarda (`applied=false`) pero no retrocede ni duplica nada. Un estado final no cambia.
- Consulta: `GET /api/v1/calls` y `GET /api/v1/calls/{id}` (con eventos). El interlocutor aparece solo como hash.
- Salientes: la plataforma pide a Twilio los avisos de estado en `POST /telephony/status`. **Entrantes:** configura en el número de Twilio *Status Callback URL* = `<TELEPHONY_PUBLIC_URL>/telephony/status` (método POST); sin eso solo se registran los hitos propios (CONNECTED/COMPLETED/TRANSFERRED).
- Una segunda entrega del webhook de una llamada ya conectada o terminada se rechaza (`telephony.call_duplicate`).
- Métrica: `omnivoice_call_events_total{status,outcome}` con `outcome` = applied | ignored | duplicate.

## Transferencia a un operador
- La transfiere el agente (herramienta `transfer_to_human`) o una persona desde el panel/API: `POST /api/v1/calls/{id}/transfer` (cuerpo opcional `{"reason": "..."}`, permiso `telephony:call`). Solo una llamada atendida (ANSWERED/CONNECTED) y con `HUMAN_TRANSFER_NUMBER` configurado; si no, 409.
- **Contexto:** al contestar el operador, Twilio pide `POST /telephony/whisper?h=<token>` y le lee, solo a él, el motivo y los últimos intercambios (hasta ~600 caracteres). El cliente no lo oye.
- **Privacidad:** ese texto contiene datos personales, así que **no se guarda en PostgreSQL**: vive 5 minutos en Redis/memoria, un solo uso. En la base solo queda el evento `TRANSFERRED` con `{"source", "context"}`. Si el operador no contesta en 5 minutos o ya se leyó, la llamada se conecta sin briefing.
- La llamada transferida queda en estado final `TRANSFERRED`; el `completed` posterior de Twilio no lo cambia.
- Finalizar: `POST /api/v1/calls/{id}/end` (cuelga; si aún no se contestó, la cancela → `CANCELLED`).

## Llamadas salientes
`POST /api/v1/outbound-calls` (roles owner/admin/manager) crea un **trabajo**; el antiguo `POST /api/v1/telephony/calls` está obsoleto y aplica las mismas reglas.
- **Consentimiento obligatorio:** `consent_basis` = `explicit_opt_in` (exige `consent_ref`: cómo y cuándo consintió) | `existing_customer` | `transactional`. Sin él, 422. La plataforma registra lo que declaras; **la validez legal de esa base es responsabilidad del cliente** y debe revisarse por país (Chile, RGPD, etc.).
- **Horario:** solo se marca dentro de `OUTBOUND_WINDOW_START/END` y `OUTBOUND_DAYS`, en la zona horaria del destinatario (`timezone`, por defecto `OUTBOUND_TIMEZONE`). Fuera de ventana el trabajo se programa para el próximo inicio de ventana; la API no puede ampliarla.
- **Reintentos:** tras ocupado, sin respuesta o fallo se reintenta (espera 15, 30, 45… min, tope 2 h) hasta `max_attempts` (por defecto 3) y queda `EXHAUSTED`. Una llamada atendida no se reintenta.
- **Duplicados:** un número no puede tener dos trabajos activos (409, garantizado por índice único); `idempotency_key` repite la misma respuesta sin crear otro.
- **Lista de exclusión:** `POST /api/v1/do-not-call {"number": ...}` cancela lo programado y bloquea nuevos trabajos (403). Solo se guarda la huella.
- **Privacidad:** el número completo se guarda únicamente mientras el trabajo está activo; al terminar se borra y queda una huella con sal por organización. La API nunca devuelve el número completo (`to_masked`).
- **Número de origen:** el de la organización (el primero con un agente publicado, o `from_number`). El agente que atiende al contestar es el de ese número.
- **Planificador:** cada `OUTBOUND_POLL_SECONDS` toma los trabajos vencidos de forma atómica (varias réplicas no se pisan; uno abandonado "marcando" se recupera tras `OUTBOUND_STALE_CLAIM_SECONDS`). Métrica `omnivoice_outbound_jobs_total{event}`. Para que los reintentos se cierren hay que configurar el *Status Callback* (ver «Estados de llamada»); las salientes lo reciben automáticamente.

## Operador por línea y proveedor
- Cada número puede tener su operador: `PATCH /api/v1/phone-numbers/{id}` con `{"transfer_number": "+56..."}` (`null` = usar `HUMAN_TRANSFER_NUMBER`). La transferencia usa el de la línea por la que entró o salió la llamada. **Quien tenga `phones:write` decide a qué número se desvían llamadas**: restringe ese permiso, porque un destino de tarificación especial sería un fraude de coste.
- `TelephonyProvider` (`app/telephony/provider.py`) es el contrato de las órdenes al proveedor. Solo cubre el plano de control; la entrada (webhooks, TwiML, firma) sigue siendo de Twilio.
