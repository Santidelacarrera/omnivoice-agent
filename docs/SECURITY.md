# Seguridad

Resumen de los controles que existen hoy y de lo que **no** existe. No se declara ninguna certificación ni cumplimiento normativo: no se ha verificado formalmente (ver `docs/PRIVACIDAD.md` y `docs/auditoria-seguridad-2026-11-05.md`).

## Autenticación y autorización
- **Personas:** JWT de un proveedor de identidad externo con claims obligatorios (`sub`, `org`, `role`, `exp`). No hay alta de usuarios, contraseñas, MFA ni recuperación de cuentas en la plataforma (pendiente: decidir entre delegarlo al proveedor de identidad o un módulo propio).
- **Integraciones:** claves de API `ovk_<prefijo>_<secreto>`. Solo se guarda el SHA-256; se muestran una vez; rol propio sin `owner` y nunca superior al de quien la crea; vencimiento y revocación; una clave no puede gestionar claves ni webhooks. Cualquier fallo devuelve el mismo 401.
- **Roles y permisos:** owner, admin, manager, operator, analyst, viewer, customer, definidos en `backend/app/security/auth.py`. Cada endpoint declara el permiso que exige.
- **WebSocket:** el JWT nunca va en la URL; se canjea por un ticket de 30 s y un solo uso, ligado al usuario, y se comprueba el `Origin`.

## Aislamiento entre organizaciones
RLS forzada en PostgreSQL con un rol sin privilegios (ver `docs/DATABASE_SCHEMA.md`). Hay pruebas de aislamiento por recurso a nivel de API y de base de datos. Un recurso ajeno responde 404, no 403, para no confirmar su existencia.

## Operaciones sensibles
- **El modelo propone, el backend dispone** (ADR-2): validación estricta de argumentos, permisos por rol, tiempo límite, idempotencia por llamada.
- **Confirmación explícita impuesta por el servidor** para reservar, modificar y cancelar (ver `docs/AI_ORCHESTRATION.md`). Límite conocido: verifica que el usuario respondió, no qué respondió.
- Las reglas de plataforma del prompt son una defensa, no una garantía.

## Webhooks y SSRF
Solo https, sin credenciales en la URL, solo direcciones públicas (comprobado al registrar y al enviar, con la conexión fijada a la IP validada), sin seguir redirecciones, errores sin detalles internos. Firma HMAC-SHA256 con marca de tiempo. Secreto cifrado en reposo (Fernet) con `WEBHOOK_SECRET_KEY` (si falta, se deriva de `JWT_SECRET`: define una propia en producción).

## Datos personales
Los números de teléfono no se guardan en claro en `calls` (hash corto) ni en las exclusiones; el número de un trabajo saliente se borra al terminar. El contexto de una transferencia no se guarda en la base (5 min, un solo uso, en Redis). Los eventos de webhook y la analítica no llevan datos personales. Retención y supresión: `docs/PRIVACIDAD.md`.

## Cabeceras y transporte
CSP, HSTS y TLS automático con Caddy en el overlay de producción; límite de peticiones por usuario y por organización; CORS acotado.

## Cadena de suministro y CI
`ruff` (incluye reglas de seguridad), `pip-audit` y `npm audit` en cada PR, Trivy en el release, Dependabot. La cobertura mínima de las pruebas unitarias es 80 %.

## Lo que falta
MFA y recuperación de cuentas; rotación automática de secretos (el secreto de un webhook sí se puede rotar a mano); cifrado de credenciales de integraciones (aún no existen); análisis estático/dinámico más allá de ruff y pip-audit; pruebas de penetración; plan de respuesta a incidentes; copias de seguridad probadas en un entorno real (los scripts existen, ver `docs/RUNBOOK.md`).

## Reportar una vulnerabilidad
Abre un aviso de seguridad privado en el repositorio de GitHub (pestaña *Security*) en lugar de un issue público.

## Conocimiento, planes y equipo
- Documentos: tipo por firma (no por extensión), límite de 5 MB y de 25 MB descomprimidos, 200 páginas PDF, `defusedxml` con DTD prohibida; el contenido recuperado se trata como dato, no como instrucciones (regla 7 de la política).
- Plan: la aplicación no puede modificarlo (solo `SELECT (plan)`); no hay autoservicio de cambio de plan.
- Equipo: la aplicación no lee `users`/`memberships`; solo funciones acotadas a una organización. Las claves de API no gestionan personas. Riesgo conocido: sin MFA ni invitaciones con verificación de email (identidad externa).
