# Cambios

Formato basado en [Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/). Aún no hay versiones etiquetadas de la capa de plataforma; todo lo siguiente está **sin publicar**.

## [Sin publicar]

### Añadido
- **Plataforma:** roles owner/admin/manager/operator/analyst/viewer; agentes versionados (editar, versiones, rollback, publicar, archivar, duplicar historial); números telefónicos únicos con agente y operador de transferencia por línea; alta de organizaciones y miembros por CLI de operador; migraciones con versión (`python -m app.migrate`); claves de API.
- **Telefonía:** enrutado por número marcado; registro de llamadas con 11 estados e idempotencia; transferencia a humano con contexto efímero; finalización por API; llamadas salientes con consentimiento obligatorio, horario, reintentos, duplicados imposibles y lista de exclusión; contrato `TelephonyProvider`.
- **IA y herramientas:** reglas base de plataforma; simulador de conversaciones sobre datos de demostración; confirmación explícita impuesta por el servidor; `modify_reservation`, `cancel_reservation`, `end_call`.
- **API:** `GET /usage`, `GET /analytics`; webhooks salientes firmados con cola, reintentos, secreto cifrado, protección SSRF, desactivación automática y purga.
- **Panel:** páginas de Llamadas y Números; analítica de 30 días en el Panel.
- **Calidad:** cobertura con umbral del 80 % en CI; pruebas de integración contra PostgreSQL y Redis reales; documentación (`API_DOCUMENTATION`, `DATABASE_SCHEMA`, `SECURITY`, `TESTING`, `TELEPHONY_INTEGRATION`, `AI_ORCHESTRATION`, `CONTRIBUTING`) y auditoría técnica con roadmap.
- Migraciones 005 a 013.

### Cambiado (incompatible)
- `POST /api/v1/telephony/calls` está **obsoleto**: exige `consent_basis`, solo lo usan owner/admin/manager y aplica las reglas de las salientes. Usa `POST /api/v1/outbound-calls`.
- `create_reservation` ya no se ejecuta en la primera llamada del modelo: el servidor exige que el agente lea los datos y el usuario responda.
- Las bases creadas por `initdb` necesitan `python -m app.migrate baseline --through 004` antes del primer `up`.
- Dependencias nuevas: `tzdata`, `cryptography>=50`.

### Seguridad
- `cryptography` 50 corrige PYSEC-2026-3552.
- RLS forzada en todas las tablas nuevas; las funciones `SECURITY DEFINER` están acotadas a una operación cada una.

### Conocido
- La confirmación de acciones verifica que el usuario respondió, no qué respondió.
- La telefonía solo se probó con Twilio simulado; el simulador, con un modelo de prueba; el panel no tiene pruebas automáticas de interfaz.
- Falta: MFA, base de conocimiento con documentos, facturación, asistente de creación de agentes, infraestructura de staging/producción y piloto (ver el roadmap).
