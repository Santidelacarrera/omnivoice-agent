# API (v1)

Base: `/api/v1`. Autenticación: `Authorization: Bearer <JWT>` (emitido por tu proveedor de identidad) o `Bearer ovk_...` (clave de API). Errores: JSON `{"detail": "..."}`; 401 credencial inválida, 403 permiso insuficiente, 404 no existe **o es de otra organización**, 422 validación, 429 límite.

## Claves de API
`POST /api/v1/api-keys {"name", "role": "admin|manager|operator|analyst|viewer", "expires_in_days"?}` (owner/admin, con JWT). Devuelve la clave **una sola vez**. `GET /api/v1/api-keys` lista prefijo, rol, último uso y estado (nunca la clave). `DELETE /api/v1/api-keys/{id}` revoca.
Reglas: no se puede crear una clave con más permisos que los propios, nunca `owner`, y una clave de API no puede gestionar claves. Las acciones hechas con una clave quedan en la auditoría como `apikey:<id>`.

## Recursos
| Recurso | Endpoints | Permiso |
|---|---|---|
| Agentes | `GET/POST /agents`, `GET/PATCH /agents/{id}`, `GET /agents/{id}/versions`, `POST /agents/{id}/rollback\|publish\|archive\|simulate` | `agents:read` / `agents:write` / `agents:publish` |
| Números | `GET/POST /phone-numbers`, `PATCH/DELETE /phone-numbers/{id}` | `phones:read` / `phones:write` |
| Llamadas | `GET /calls`, `GET /calls/{id}`, `POST /calls/{id}/transfer\|end` | `calls:read` / `telephony:call` |
| Salientes | `POST/GET /outbound-calls`, `GET /outbound-calls/{id}`, `POST /outbound-calls/{id}/cancel`, `POST/GET /do-not-call`, `DELETE /do-not-call/{id}` | `outbound:write` / `calls:read` |
| Conversaciones | `GET /conversations[/{id}]`, `DELETE /conversations/{id}`, `GET /conversations/{id}/recording` | `conversations:read\|delete`, `recordings:read` |
| Auditoría y métricas | `GET /audit-logs`, `GET /metrics` | `audit:read`, `metrics:read` |

Detalle de reglas (consentimiento, horarios, confirmación de acciones, privacidad) en `docs/RUNBOOK.md` y `docs/AI_ORCHESTRATION.md`. La especificación OpenAPI completa está en `/docs` fuera de producción.

## Pendiente respecto al documento maestro
`POST /api/v1/calls` (crear llamada: hoy es `POST /outbound-calls`), `GET /api/v1/usage` y `GET /api/v1/analytics`, webhooks salientes firmados y SDK.
