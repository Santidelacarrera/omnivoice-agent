# Esquema de base de datos

PostgreSQL 16. Las migraciones están en `migrations/NNN_*.sql` y se aplican con `python -m app.migrate up` (ver `docs/RUNBOOK.md`). El estado de cada una queda en `schema_migrations` (versión, checksum, fecha).

## Aislamiento entre organizaciones
Toda tabla con datos de una organización tiene `org_id`, **RLS activada y forzada** y la política `org_isolation` (`org_id = current_setting('app.org_id')::uuid`). La aplicación se conecta como `omni_app` (sin superusuario ni `BYPASSRLS`) y cada operación fija `app.org_id` en su transacción (`org_transaction`). Si falta, la política falla cerrada: error o cero filas, nunca datos de otra organización. `omni_app` no tiene acceso a `users` ni `memberships`, y solo lee columnas concretas de `organizations`.

**Funciones `SECURITY DEFINER`** (únicas excepciones, cada una acotada a una operación y con `EXECUTE` solo para `omni_app`): `resolve_phone_number` (número → organización/agente), `resolve_call_org` (id de llamada → organización), `resolve_api_key` (prefijo → clave), `claim_due_outbound_jobs` y `claim_due_webhook_deliveries` (reclamo atómico entre réplicas con `SKIP LOCKED`), `purge_webhook_deliveries`. Requieren que su dueño (el rol de migraciones) no esté sujeto a RLS.

## Tablas
| Dominio | Tabla | Notas |
|---|---|---|
| Identidad | `organizations`, `users`, `memberships` | roles: owner, admin, manager, operator, analyst, viewer, customer. Sin contraseñas: la identidad es externa (JWT). Gestión por CLI de operador. |
| Agentes | `agents`, `agent_tools`, `agent_versions` | `status` draft/published/archived; cada cambio crea una versión inmutable (solo `SELECT, INSERT`). |
| Telefonía | `phone_numbers` | `e164` único en toda la plataforma; `agent_id`, `transfer_number` opcionales. |
| | `calls`, `call_events` | una fila por `(provider, provider_call_sid)`; 11 estados; eventos solo-anexar con `event_key` único por llamada (idempotencia). El interlocutor solo como hash. |
| | `outbound_jobs`, `do_not_call` | consentimiento obligatorio, ventana horaria, reintentos; el número se borra al terminar; índice único parcial impide dos trabajos activos para el mismo número. La exclusión guarda solo la huella (SHA-256 con sal por organización). |
| Conversación | `sessions`, `conversations`, `conversation_events`, `transcripts`, `recordings` | transcripciones y grabaciones sujetas a `organizations.retention_days`; supresión por conversación. |
| Herramientas | `tool_executions` | idempotente por `(org_id, call_id, tool_name)`; no guarda argumentos, solo sus claves. |
| Negocio de ejemplo | `inventory`, `reservations`, `support_tickets`, `knowledge_base` | `reservations.status` confirmed/cancelled (cancelar conserva la fila). Búsqueda de texto completo en español en `knowledge_base`. |
| Seguridad | `api_keys` | solo `key_hash` (SHA-256) y `prefix`; rol sin `owner`; se revoca, no se borra. |
| Integración | `webhook_endpoints`, `webhook_deliveries` | secreto cifrado (`secret_enc`); entregas con reintentos; purga a los 14 días de lo terminado. |
| Auditoría y consumo | `audit_logs`, `usage_records` | `audit_logs` es solo-anexar para la aplicación (`REVOKE UPDATE, DELETE`). |
| Migraciones | `schema_migrations` | creada por `app.migrate`; la aplicación no tiene acceso. |

## Pendiente respecto a la sección 20 del documento maestro
`roles`/`permissions` como tablas (hoy son código en `security/auth.py`), `telephony_providers`, `integrations`, `knowledge_sources` (documentos y fragmentos con embeddings), `subscriptions` e `invoices` (facturación), `messages` como entidad propia.

## Migraciones 016–018
| Migración | Contenido |
|---|---|
| 016 | `kb_sources`, `kb_chunks` (tsvector generado, RLS por organización). |
| 017 | `organizations.plan`; la aplicación solo puede leerlo (`GRANT SELECT (plan)`); lo cambia el operador con `python -m app.cli set-plan`. |
| 018 | Funciones `SECURITY DEFINER` `team_list`, `team_set_member`, `team_remove_member`, acotadas a una organización, con las reglas de owner dentro de la función. |

| 019 | `integrations`: conectores por organización con `credential_enc` (Fernet), `allowed_methods`, `allowed_paths`, RLS por organización, nombre único por organización. |
