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
- [x] Transferencia a humano con contexto (briefing de motivo + últimos intercambios dicho solo al operador vía `<Number url>`, de un solo uso, efímero: no se guarda en la BD) y `POST /api/v1/calls/{id}/transfer|end`.
- [x] Llamadas salientes controladas: trabajos (`outbound_jobs`, migración 008) con base de consentimiento obligatoria, ventana horaria en la zona del destinatario, reintentos con espera creciente, huella del número con sal por organización (el número se borra al terminar), duplicados imposibles (índice único + idempotencia), lista de exclusión (`do_not_call`, solo huellas), planificador atómico entre réplicas, número de origen por organización.
- [x] Operador de transferencia por línea (`phone_numbers.transfer_number`, migración 009; `PATCH /api/v1/phone-numbers/{id}`); sin valor usa `HUMAN_TRANSFER_NUMBER`.
- [x] Contrato `TelephonyProvider` (`app/telephony/provider.py`) para las órdenes salientes al proveedor (marcar, desviar, colgar). **Alcance limitado:** el webhook de entrada, el formato TwiML y la firma siguen siendo específicos de Twilio; un segundo proveedor necesita además un adaptador de entrada.
- [ ] Pendiente: adaptador de entrada genérico para un segundo proveedor/SIP; campañas masivas y límites de coste por plan; lista de destinos de transferencia permitidos (riesgo de fraude por números de tarificación especial); revisión legal por país del texto de consentimiento y horarios (los valores por defecto no son asesoría legal).
- [ ] Evaluar SIP directo (verificar condiciones del proveedor antes).

## ETAPA 4 — IA
- [x] Reglas base de plataforma antepuestas a todo agente (incertidumbre, no inventar, confirmación, sin consejo médico/legal, memoria por llamada) y modelo de memoria de tres capas documentado (`docs/AI_ORCHESTRATION.md`).
- [x] Simulador `POST /api/v1/agents/{id}/simulate` (borradores incluidos, herramientas sobre sandbox en memoria, acotado y con límite por organización).
- [ ] Pendiente: evaluaciones automáticas de calidad conversacional (conjuntos de casos con resultados esperados) y medir tasa de alucinación; el simulador hoy es manual.

## ETAPA 5 — Herramientas
- [x] Herramientas `modify_reservation`, `cancel_reservation` (verifican el nombre de la reserva; cancelar conserva la fila; migración 010) y `end_call` (el agente cuelga tras despedirse). `transfer_to_human` ya existía.
- [ ] Pendiente: consultar/registrar clientes, enviar confirmaciones (SMS/WhatsApp/email) y consultar pedidos; requieren integraciones y tablas que aún no existen.
- [x] Confirmación explícita aplicada por el servidor (`Tool.needs_confirmation`): la acción solo se ejecuta si, tras pedirla, el agente habló y el usuario terminó de hablar, con argumentos idénticos y de un solo uso. El simulador la aplica igual.
- [ ] Limitación conocida: el servidor verifica que el usuario respondió, no el contenido de su respuesta (un «no» también habilita el reintento). Mitigación posible: confirmación por segundo canal o clasificación de la respuesta.

## ETAPA 6 — Panel SaaS
- [x] Páginas de **Llamadas** (lista, eventos, transferir y finalizar), **Números** (alta, agente, operador por línea) y analítica de 30 días en el **Panel**. Verificadas con typecheck, build y carga real contra el backend; sin pruebas automatizadas de interfaz.
- [ ] Pendiente: asistente de 9 pasos para crear agentes, salientes y lista de exclusión, claves de API, webhooks, equipo, facturación, diseño responsive revisado y accesibilidad auditada, Playwright. La autenticación del panel sigue siendo el token de desarrollo: hace falta el flujo de identidad real antes de producción.

## ETAPA 7 — Conocimiento
- [ ] Ingesta PDF/DOCX/TXT/CSV, fragmentos, embeddings (pgvector), citas, versionado.

## ETAPA 8 — Seguridad y privacidad
- [x] API keys (`ovk_<prefijo>_<secreto>`, solo hash SHA-256, mostradas una vez, rol acotado al del creador y nunca owner, vencimiento, revocación, una clave no gestiona claves; migración 011).
- [ ] Pendiente: MFA y recuperación de cuentas (la identidad es externa: se resuelve en el proveedor de identidad o con un módulo de usuarios propio, decisión de producto), cifrado de credenciales de integraciones (aún no hay integraciones), rotación de secretos, SAST/DAST en CI.

## ETAPA 9 — Analítica y facturación
- [ ] `subscriptions`, `invoices`, cuotas por plan, margen por organización, Grafana, OpenTelemetry, Sentry.

## ETAPA 10 — API pública y webhooks
- [x] `GET /api/v1/usage` y `GET /api/v1/analytics` (llamadas, atendidas, perdidas, transferencias, duración media, por día y por agente; agregados en SQL).
- [x] Webhooks salientes firmados (HMAC-SHA256 con marca de tiempo), cola en la base de datos con reintentos (30 s, 2 min, 10 min, 1 h, 6 h), secreto cifrado en reposo y rotable, protección SSRF (solo https, solo IP públicas, comprobada al registrar y al enviar, conexión fijada a la IP validada, sin seguir redirecciones). Eventos: `call.started|completed|failed|transferred`, `tool.executed`, `webhook.test`. Migración 012.
- [ ] Pendiente: `POST /api/v1/calls` (alias de salientes), evento de alerta de consumo (necesita cuotas por plan, Etapa 9), SDK y ejemplos de integración, (la purga de entregas a los 14 días y la desactivación automática tras 10 fallos seguidos ya están hechas, migración 013).

## ETAPA 11–13
- [ ] Carga y recuperación; staging/producción con aprobación; piloto con 3–5 clínicas.
- [x] Documentos: API_DOCUMENTATION, DATABASE_SCHEMA, SECURITY, TESTING, TELEPHONY_INTEGRATION, AI_ORCHESTRATION, CONTRIBUTING, CHANGELOG.
- [ ] Faltan: BILLING (no hay facturación todavía), TECHNICAL_SPECIFICATION y DEPLOYMENT dedicado (hoy en RUNBOOK y README).
