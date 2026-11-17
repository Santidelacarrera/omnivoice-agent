# OmniVoice Agent — Roadmap de implementación

Base: `OMNIVOICE_TECHNICAL_AUDIT.md`. Regla: no reconstruir lo ✅; cada tarea se cierra con pruebas (incl. aislamiento entre organizaciones).

## ETAPA 1 — Auditoría (hecha)
- [x] Auditoría y roadmap.

## ETAPA 2 — Fundamentos de plataforma  ← en curso
- [x] T2.1 Roles Owner/Admin/Manager/Operator/Analyst/Viewer (+ `customer` existente) y permisos nuevos en `security/auth.py`.
- [x] T2.2 Migración `005_platform.sql`: ampliar `memberships.role`; tablas `agent_versions`, `phone_numbers`; columnas `agents.status/version/phone`; RLS forzada + grants.
- [x] T2.3 Agentes: `GET /agents/{id}`, `PATCH /agents/{id}` (crea versión), `GET /agents/{id}/versions`, `POST /agents/{id}/rollback`, `POST /agents/{id}/archive`, `POST /agents/{id}/publish`.
- [x] T2.4 Números telefónicos: `GET/POST /phone-numbers`, `DELETE`; E.164 único global (un número = una organización), agente asociado de la misma organización.
- [x] T2.5 Pruebas: permisos por rol, versionado/rollback, aislamiento entre organizaciones, unicidad de números.
- [x] T2.6 Alta de organizaciones y usuarios: CLI de operador (`create-org`, `set-member`, `list-members`) con `ADMIN_DATABASE_URL`; deliberadamente NO por HTTP, porque `omni_app` no puede tocar `users`/`memberships` (último owner protegido, auditado).
- [x] T2.7 Migraciones con versión: `python -m app.migrate status|up|baseline` (tabla `schema_migrations`, checksum inmutable, transacción por archivo, candado consultivo, `baseline` obligatorio para bases previas). CI lo usa en lugar del bucle `psql`.
- [x] T2.8 Cobertura (líneas + ramas) medida en CI con umbral 80 % en pruebas unitarias (hoy 81,4 %); la de integración se publica sin umbral. Pendiente: combinar ambas y subir el umbral, y cubrir `state_store` Redis, `telephony/twilio.py` y `realtime/provider.py` (OpenAI), los más bajos.

## ETAPA 3 — Telefonía
- [x] Usar `phone_numbers` en `/telephony/voice` para resolver organización y agente; solo atiende un agente `published` (borrador, archivado o sin agente → mensaje y colgado, con auditoría). Función `resolve_phone_number` (migración 006, SECURITY DEFINER, solo búsqueda exacta). `TELEPHONY_ALLOW_UNMAPPED=false` rechaza números sin asignar.
- [ ] Pendiente de esta línea: el número de origen de las llamadas salientes (`TWILIO_FROM_NUMBER`) sigue siendo global; debe salir del número de la organización.
- [x] Tablas `calls`/`call_events` (migración 007, RLS, eventos solo-anexar), estados CREATED…TRANSFERRED con transiciones solo hacia adelante, idempotencia por `(proveedor, CallSid)` y por evento, `POST /telephony/status` firmado, `GET /api/v1/calls[/{id}]`, métrica `omnivoice_call_events_total`, rechazo de entregas duplicadas de una llamada ya atendida.
- [ ] Pendiente: interfaz `TelephonyProvider` (abstracción multi-proveedor; hoy el código conoce Twilio) y configurar el Status Callback en el número para entrantes (Twilio solo lo envía si está configurado en la consola/número).
- [ ] Transferencia a humano con contexto; `POST /calls/{id}/transfer|end`; llamadas salientes con horarios, reintentos y consentimiento.
- [ ] Evaluar SIP directo (verificar condiciones del proveedor antes).

## ETAPA 4 — IA
- [ ] Memoria separada (llamada / cliente / conocimiento) y reglas de incertidumbre en prompt base.
- [ ] Entorno de simulación de conversaciones para agentes en borrador.

## ETAPA 5 — Herramientas
- [ ] Modificar/cancelar reserva, clientes, confirmaciones, finalizar/transferir llamada.
- [ ] Confirmación explícita en acciones sensibles.

## ETAPA 6 — Panel SaaS
- [ ] Asistente de 9 pasos, números, llamadas en vivo, equipo, auditoría; Playwright.

## ETAPA 7 — Conocimiento
- [ ] Ingesta PDF/DOCX/TXT/CSV, fragmentos, embeddings (pgvector), citas, versionado.

## ETAPA 8 — Seguridad y privacidad
- [ ] MFA, API keys con hash, cifrado de credenciales de integraciones, rotación, SAST en CI.

## ETAPA 9 — Analítica y facturación
- [ ] `subscriptions`, `invoices`, cuotas por plan, margen por organización, Grafana, OpenTelemetry, Sentry.

## ETAPA 10 — API pública y webhooks
- [ ] `/calls`, `/usage`, `/analytics`, webhooks firmados, SDK y documentación.

## ETAPA 11–13
- [ ] Carga y recuperación; staging/producción con aprobación; piloto con 3–5 clínicas.
- [ ] Documentos faltantes: API_DOCUMENTATION, DATABASE_SCHEMA, SECURITY, TESTING, BILLING, CONTRIBUTING, CHANGELOG.
