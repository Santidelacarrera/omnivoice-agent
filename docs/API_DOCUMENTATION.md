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
| Pedidos y clientes | `PUT /orders/{id}`, `GET /customers`, `DELETE /customers/{id}` | `orders:write` (owner/admin/manager) / `customers:write` (owner/admin) |
| Llamadas (alias) | `POST /api/v1/calls` equivale a `POST /outbound-calls` y añade `call_sid` | `outbound:write` |
| Auditoría y métricas | `GET /audit-logs`, `GET /metrics` | `audit:read`, `metrics:read` |

Detalle de reglas (consentimiento, horarios, confirmación de acciones, privacidad) en `docs/RUNBOOK.md` y `docs/AI_ORCHESTRATION.md`. La especificación OpenAPI completa está en `/docs` fuera de producción.

## SDK
Python en `sdk/python` (un archivo, depende solo de `httpx`; ver su README). Pendiente: SDK de JavaScript y publicación en PyPI y npm.

## Pendiente respecto al documento maestro
Alertas de consumo por webhook (necesitan cuotas por plan).

## Uso y analítica
`GET /api/v1/usage?days=30` (minutos de audio, coste estimado de voz, llamadas y coste por llamada; **estimación, no factura**) y `GET /api/v1/analytics?days=30` (llamadas totales/entrantes/salientes, atendidas, perdidas, transferidas, duración media, tasas, serie por día y por agente). `days` entre 1 y 365. Permiso `metrics:read`; solo datos de tu organización y sin datos personales.

## Webhooks
Registro (owner/admin, con JWT; una clave de API no puede gestionarlos): `POST /api/v1/webhooks {"url": "https://...", "events": ["call.completed", ...] | ["*"], "description"?}`. Devuelve el **secreto de firma una sola vez** (`whsec_...`); se guarda cifrado. Otros: `GET`, `PATCH /{id}` (`url`, `events`, `active`), `DELETE /{id}`, `POST /{id}/rotate-secret`, `POST /{id}/test`, `GET /{id}/deliveries`.

**Eventos:** `call.started` (conectada), `call.completed`, `call.failed` (ocupado, sin respuesta, fallo o cancelada; trae `status`), `call.transferred`, `tool.executed` (nombre, resultado y duración; nunca argumentos), `webhook.test`. Los eventos llevan identificadores y estados, **no** números de teléfono, transcripciones ni datos del cliente.

**Entrega:** `POST` JSON `{"id", "type", "created", "org_id", "data"}` con cabeceras `X-OmniVoice-Event`, `X-OmniVoice-Delivery` (= `id`) y `X-OmniVoice-Signature: t=<unix>,v1=<hex>`. Responde 2xx en menos de 5 s. Si no, se reintenta a los 30 s, 2 min, 10 min, 1 h y 6 h y después se da por fallida (visible en `/deliveries`). Es **al menos una vez**: deduplica por `id`.

**Verificar la firma** (rechaza si falla o si `t` tiene más de 5 minutos):
```python
import hmac, hashlib, time
def verify(secret, header, body, tolerance=300):
    p = dict(x.split("=", 1) for x in header.split(","))
    ok = hmac.compare_digest(hmac.new(secret.encode(), f"{p['t']}.{body}".encode(), hashlib.sha256).hexdigest(), p["v1"])
    return ok and abs(time.time() - int(p["t"])) <= tolerance
```
Usa el cuerpo **crudo** tal como llega. **Seguridad:** la URL debe ser https, sin credenciales, y resolver solo a direcciones públicas; no se siguen redirecciones. Variables: `WEBHOOK_SECRET_KEY` (cifrado de secretos; si falta se deriva de `JWT_SECRET`, y rotar esa clave dejaría ilegibles los secretos guardados: define una propia en producción), `WEBHOOKS_ALLOW_PRIVATE` (**solo desarrollo**: desactiva la defensa SSRF).

## Conocimiento, plan y equipo
- `POST/PUT/GET/DELETE /api/v1/knowledge/sources` y `GET /api/v1/knowledge/search?q=` (permisos `knowledge:read/write`): documentos en base64 (máx. 5 MB); PDF, DOCX, TXT y CSV; el tipo se detecta por firma. La búsqueda usa texto completo en español (no semántica: «devolver» no enlaza con «devoluciones»).
- `GET /api/v1/plan`: plan, límites, consumo y porcentaje. Con `PLANS_ENFORCED=true` las operaciones que superan el límite devuelven 409 (cuotas) o 429 (minutos del mes). Webhook `usage.alert` al 80 y 100 %.
- `GET /api/v1/team`, `PUT /api/v1/team/{email}` (`{"role": ...}`), `DELETE /api/v1/team/{email}`: requieren `team:read`/`team:write` (owner y admin); no las usan claves de API. 403 `owner_only`, 409 si se quedaría sin owner. Esta lista no autentica: el proveedor de identidad debe consultarla al emitir el JWT.
