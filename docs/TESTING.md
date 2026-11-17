# Pruebas

## Niveles
| Nivel | Dónde | Qué cubre | Requiere |
|---|---|---|---|
| Unitarias y de API | `backend/tests/test_*.py` | lógica pura, API con `TestClient`, persistencia y estado en memoria, proveedor de voz y Twilio simulados | nada |
| Integración | `backend/tests/integration/` | PostgreSQL y Redis **reales**: RLS, funciones `SECURITY DEFINER`, concurrencia (`SKIP LOCKED`), flujos completos | `TEST_DATABASE_URL`, `TEST_REDIS_URL`, `TEST_ADMIN_DATABASE_URL` |
| Contrato | `backend/tests/contract/` | APIs reales de OpenAI, Gemini, Deepgram, Anthropic | sus claves (se omiten sin ellas) |
| Rendimiento | `backend/bench/` | latencia por etapa, carga, SLO (`docs/slo.json`) | ver `docs/LATENCY.md` |

No hay pruebas automatizadas de interfaz (Playwright está pendiente). El frontend se comprueba con `npm run typecheck` y `npm run build`.

## Ejecutarlas
Unitarias, con cobertura (el CI exige 80 % de líneas y ramas):
```bash
cd backend && python -m pytest -q --ignore=tests/integration --cov --cov-fail-under=80
```
Integración, con servicios reales en Docker:
```bash
docker run -d --name ov-pg -e POSTGRES_USER=omni -e POSTGRES_PASSWORD=omni -e POSTGRES_DB=omnivoice -p 55432:5432 postgres:16-alpine
docker run -d --name ov-redis -p 56379:6379 redis:7-alpine
cd backend
export ADMIN_DATABASE_URL=postgresql+asyncpg://omni:omni@localhost:55432/omnivoice
export TEST_ADMIN_DATABASE_URL=$ADMIN_DATABASE_URL
export TEST_DATABASE_URL=postgresql+asyncpg://omni_app:omni_app@localhost:55432/omnivoice
export TEST_REDIS_URL=redis://localhost:56379/0
python -m app.migrate up && python -m pytest -q
```
Las variables deben estar en **el mismo shell** que ejecuta `pytest`; si no, las pruebas de integración se omiten sin avisar (aparecen como *skipped*).

## Qué se exige de cada módulo
Un módulo no se da por terminado por existir su código: lleva pruebas unitarias, y si toca la base de datos, una de integración contra PostgreSQL real (las dos únicas fallas de PostgreSQL que tuvo el proyecto —parámetros nulos sin tipo y fechas— solo se vieron así). Todo recurso por organización lleva una prueba de que otra organización no lo ve ni lo modifica.

## Trampas conocidas
- `TestClient` sin `with` crea y cierra un bucle de eventos por petición: las tareas de fondo se cancelan. Usa `with TestClient(...)` (o `c.__enter__()`) cuando el comportamiento dependa de tareas diferidas, y `c.portal.call(...)` para ejecutar corrutinas que usan el motor de PostgreSQL (está atado a ese bucle; `asyncio.run` falla con «Event loop is closed»).
- `test_full_stack.py` solo pasa con la base vacía (toma la primera conversación del listado antes de que se guarde la nueva). En CI la base es nueva; en local, vacía `sessions` y `tool_executions`.
- Las pruebas que dependen de la hora fijan el reloj (`now=`) o abren la ventana horaria; no dependas del reloj real.
- No mezcles `now()` de PostgreSQL con el reloj de la aplicación en lógica de colas (la deriva de Docker rompe la comparación).

## Estado actual
364 pruebas con PostgreSQL y Redis reales, cobertura combinada 88 %. Falta: pruebas de carga con umbral en CI, pruebas de recuperación (caída de Redis/PostgreSQL a mitad de llamada), pruebas de interfaz y pruebas con proveedores reales de telefonía.
## Añadido después
- **Interfaz (Playwright):** `cd frontend && npm run e2e`. Levanta un backend real en memoria y el frontend compilado, y recorre el asistente de 9 pasos, números, salientes, integraciones y auditoría. Estas pruebas encontraron dos fallos reales (el CORS bloqueaba PATCH, PUT y DELETE desde el navegador y el cliente fallaba con las respuestas 204). En CI corren en el job `frontend`.
- **Recuperación:** `tests/test_recovery.py` simula la caída de Redis (un almacén que lanza el error real) y de PostgreSQL (puerto sin servidor) con llamadas en curso, y prueba el cortacircuitos.
- **Carga:** el CI ejecuta `bench.orchestrator_bench --sessions 200` con umbrales (`--max-barge-p95-us 5000`, `--max-wall-seconds 60`). Son **holgados a propósito** (la medición local ronda 5 µs): detectan regresiones grandes, no microoptimizaciones. Mide solo nuestro código con proveedor simulado, no la latencia extremo a extremo.
- **Evaluaciones conversacionales:** ver `docs/RUNBOOK.md`; `tests/test_evals.py` comprueba que cada comprobación detecta el fallo que dice detectar.
- **SDK:** `tests/test_sdk.py` ejecuta el SDK de Python contra la API real en memoria.
- **Playwright en local:** la primera vez `npx playwright install chromium` (descarga unos 90 MB).
