# Runbook

## Despliegue
1. Aplicar `migrations/001..003` con un usuario administrador de BD. En producción **cambia la contraseña de `omni_app`**
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

## Retención y borrado
`organizations.retention_days` define el periodo, pero **no hay job de purga implementado**. Hasta añadirlo, ejecutar
periódicamente `DELETE FROM transcripts WHERE ts < now() - interval '30 days'` (por organización con `SET app.org_id`).

## Pruebas de rendimiento
- Orquestador aislado (sin red): `python -m bench.orchestrator_bench --sessions 200`.
- Extremo a extremo: levantar el stack y `python -m bench.loadtest --url http://localhost:8000 --clients 50 --duration 20`.
  Con el proveedor simulado mide tu stack; con `OPENAI_API_KEY` mide también al proveedor y la red.
  Sube `--clients` hasta ver 429 o aumento de p95 para fijar la capacidad por réplica.
