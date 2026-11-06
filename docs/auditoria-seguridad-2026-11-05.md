# Auditoría de seguridad — 2026-11-05

Alcance: código, configuración y CI del repositorio, en local y con datos sintéticos. No se atacó ningún servicio en vivo.

## Hallazgos y estado

| # | Sev. | Área | Hallazgo | Estado |
|---|---|---|---|---|
| 1 | Media | Autenticación | `decode_token` aceptaba JWT firmados sin `role`/`exp` y con rol desconocido: el primer caso provocaba un 500 (`KeyError`) y los otros dejaban pasar tokens sin caducidad. (`backend/app/security/auth.py`) | Corregido: exige `exp`, `sub`, `org`, `role` y un rol conocido; test `test_token_with_missing_or_unknown_claims_is_401_not_500`. |
| 2 | Media | Origen | `/ws/audio` no comprobaba `Origin` (el ticket de un solo uso mitigaba, pero faltaba defensa en profundidad). (`backend/app/main.py`) | Corregido: fuera de `development`, un `Origin` ajeno cierra con 4401; test `test_ws_rejects_foreign_origin`. |
| 3 | Media | Cabeceras | La API no enviaba HSTS ni CSP; el frontend no tenía CSP. | Corregido: API con `CSP default-src 'none'`, `X-Frame-Options`, `Permissions-Policy` y HSTS en producción; frontend con CSP (`next.config.js`); Caddy con HSTS. |
| 4 | Baja | CSP frontend | `script-src` necesita `'unsafe-inline'` porque Next.js hidrata con scripts en línea y no hay nonce por petición. | Abierto (limitación conocida). Mitiga: sin `unsafe-eval` en producción, `object-src 'none'`, `frame-ancestors 'none'`. |
| 5 | Baja | Contenedores | Imágenes fijadas por etiqueta, no por digest. | Abierto. Dependabot (Docker) las actualiza; Trivy bloquea HIGH/CRITICAL en el release. |
| 6 | Info | Base de datos | La clave de ejemplo de `omni_app` está en `002_app_role.sql`. | Mitigado: `migrations/900_app_password.sh` la reemplaza en producción y el overlay exige `OMNI_APP_PASSWORD`. |

## Revisado sin hallazgos
- Aislamiento: RLS `FORCE` y rol `omni_app` sin superusuario ni `BYPASSRLS` (comprobado en el job `migrations` de CI).
- Secretos: búsqueda de patrones (OpenAI, AWS, Google, claves privadas) en el árbol y el historial: solo una clave de ejemplo en un test de redacción de logs.
- Tickets WS: un solo uso (`GETDEL`) y 30 s; el JWT no viaja en la URL.
- Telefonía: firma de Twilio con `hmac.compare_digest`.
- Dependencias: `pip-audit` y `npm audit` limpios.
- Contenedores: usuario no root, backend de solo lectura y sin capacidades, imagen del frontend `standalone`.
- CI: permisos `contents: read`; el release solo añade `packages: write`.
- Logs: sin audio, transcripciones ni argumentos de herramientas; credenciales redactadas (`redact_secrets`).
- Errores: respuestas genéricas; `/docs` deshabilitado y `dev-token` ausente en producción.

## Lo que NO se cubrió
- No se ejecutó `gitleaks` ni escaneo de imagen (Docker no disponible en esta máquina); lo hace Trivy en el release.
- No hubo pruebas contra los proveedores reales (OpenAI, Gemini, Twilio) ni contra una instancia desplegada.
- Sin pentest dinámico ni fuzzing; el VAD y los códecs de audio no se fuzzearon.
- Que una herramienta no encuentre más hallazgos no prueba que el sistema sea seguro.
