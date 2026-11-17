# Cambios

Formato basado en [Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/). Aún no hay versiones etiquetadas de la capa de plataforma; todo lo siguiente está **sin publicar**.

## [Sin publicar]

### Añadido
- **SDK de JavaScript** (`sdk/js`): agentes, llamadas, salientes, webhooks, equipo, plan y `verifyWebhook`; pruebas en CI.
- **Conocimiento (migración 016):** documentos PDF/DOCX/TXT/CSV con extracción segura (tipo por firma, límites anti zip-bomb, XML sin DTD), fragmentos con fuente, versiones, cuotas y búsqueda en español; página «Conocimiento» y herramienta `search_knowledge_base` (el contenido es dato, nunca instrucciones).
- **Planes y cuotas (migración 017):** planes definidos en código sin precios, cuotas opt-in (`PLANS_ENFORCED`), consumo mensual, bloqueo, webhook `usage.alert` al 80 y 100 %, `GET /api/v1/plan`, CLI `set-plan`; margen solo si se configura un precio.
- **Equipo (migración 018):** `GET/PUT/DELETE /api/v1/team`; reglas dentro de la base de datos (solo un owner toca owners, nunca sin owner); página «Equipo». La autenticación sigue siendo externa.
- **Plataforma:** roles owner/admin/manager/operator/analyst/viewer; agentes versionados (editar, versiones, rollback, publicar, archivar, duplicar historial); números telefónicos únicos con agente y operador de transferencia por línea; alta de organizaciones y miembros por CLI de operador; migraciones con versión (`python -m app.migrate`); claves de API.
- **Telefonía:** enrutado por número marcado; registro de llamadas con 11 estados e idempotencia; transferencia a humano con contexto efímero; finalización por API; llamadas salientes con consentimiento obligatorio, horario, reintentos, duplicados imposibles y lista de exclusión; contrato `TelephonyProvider`.
- **IA y herramientas:** reglas base de plataforma; simulador de conversaciones sobre datos de demostración; confirmación explícita impuesta por el servidor; `modify_reservation`, `cancel_reservation`, `end_call`.
- **API:** `GET /usage`, `GET /analytics`; webhooks salientes firmados con cola, reintentos, secreto cifrado, protección SSRF, desactivación automática y purga.
- **Panel:** páginas de Llamadas y Números; analítica de 30 días en el Panel.
- **Calidad:** cobertura con umbral del 80 % en CI; pruebas de integración contra PostgreSQL y Redis reales; documentación (`API_DOCUMENTATION`, `DATABASE_SCHEMA`, `SECURITY`, `TESTING`, `TELEPHONY_INTEGRATION`, `AI_ORCHESTRATION`, `CONTRIBUTING`) y auditoría técnica con roadmap.
- Migraciones 005 a 015.

- **Herramientas del agente:** reconocer a quien llama, registrar cliente, consultar pedidos y SMS de confirmación solo al número de quien llama (migración 015); API para importar pedidos y suprimir clientes.
- **API y SDK:** `POST /api/v1/calls` y SDK de Python.
- **Telefonía:** destinos de transferencia permitidos, vigilante de llamadas abandonadas (migración 014) y alertas del proveedor.
- **Evaluaciones conversacionales** con 6 casos y workflow manual con el modelo real.
- **Panel:** asistente de 9 pasos, salientes y exclusión, claves y webhooks, auditoría; **pruebas de interfaz con Playwright en CI**.
- **Operación:** panel de Grafana provisionado; Sentry y OpenTelemetry opcionales (sin verificar contra servicios reales).
- **Resiliencia:** 503 limpio si cae Redis o PostgreSQL, plazos de conexión, cortacircuitos de persistencia y umbrales de carga en CI.

### Cambiado (incompatible)
- `POST /api/v1/telephony/calls` está **obsoleto**: exige `consent_basis`, solo lo usan owner/admin/manager y aplica las reglas de las salientes. Usa `POST /api/v1/outbound-calls`.
- `create_reservation` ya no se ejecuta en la primera llamada del modelo: el servidor exige que el agente lea los datos y el usuario responda.
- Las bases creadas por `initdb` necesitan `python -m app.migrate baseline --through 004` antes del primer `up`.
- Dependencias nuevas: `tzdata`, `cryptography>=50` (opcionales en `requirements-optional.txt`: Sentry y OpenTelemetry).
- CORS ahora permite `PATCH`, `PUT` y `DELETE` desde los orígenes configurados (antes solo GET y POST: el panel no podía editar ni borrar).

### Seguridad
- `cryptography` 50 corrige PYSEC-2026-3552.
- RLS forzada en todas las tablas nuevas; las funciones `SECURITY DEFINER` están acotadas a una operación cada una.

### Conocido
- La confirmación de acciones verifica que el usuario respondió, no qué respondió.
- La telefonía solo se probó con Twilio simulado y el SMS con un doble de pruebas; el simulador y las evaluaciones offline, con un modelo de prueba (las evaluaciones con el modelo real son un workflow manual aún sin ejecutar); Sentry y OpenTelemetry, con módulos falsos.
- Falta: MFA, base de conocimiento con documentos, facturación, asistente de creación de agentes, infraestructura de staging/producción y piloto (ver el roadmap).
