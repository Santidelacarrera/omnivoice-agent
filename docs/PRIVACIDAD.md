# Privacidad y datos personales

> Esto no es asesoría legal. Los puntos marcados «revisión legal» deben validarlos un abogado antes del lanzamiento.
> La Ley 21.719 (Chile) entra en vigor el 1 de diciembre de 2026.

## Tabla de tratamientos

| Dato | Finalidad | Base | Ubicación | Retención | Riesgo | Acción pendiente |
|---|---|---|---|---|---|---|
| Voz del usuario (flujo en vivo) | Conversar con el agente | Ejecución del servicio solicitado | Memoria del backend → proveedor de voz (OpenAI / Gemini) o cascada STT/LLM/TTS | No se persiste | Medio: sale a un tercero | Revisión legal: contrato/DPA con el proveedor y transferencia internacional |
| Transcripciones | Historial y calidad | Ejecución del servicio | PostgreSQL (`transcripts`), aislado por organización (RLS) | `organizations.retention_days` (30 por defecto), purga automática | Medio: pueden contener datos personales dichos en voz | Informar al usuario en la interfaz; definir plazo por cliente |
| Grabación de audio | Auditoría/calidad (opcional) | **Consentimiento explícito** por sesión + habilitada por la organización | Almacenamiento S3/local (`recordings`) | Igual que las transcripciones | Alto | Texto del consentimiento revisado legalmente |
| Identificador de usuario (`sub`) y organización | Autorización y auditoría | Interés legítimo / contrato | PostgreSQL, JWT | Mientras exista la organización | Bajo | — |
| Auditoría (`audit_logs`) | Seguridad y trazabilidad | Interés legítimo | PostgreSQL, solo-anexar | No se purga; no contiene transcripciones ni argumentos de herramientas | Bajo | Definir plazo de conservación con el cliente |
| Argumentos de herramientas | — | — | **No se guardan** (solo las claves) | — | — | — |
| Teléfono de llamadas (Twilio) | Telefonía | Ejecución del servicio | Twilio; metadatos de sesión | Según Twilio y retención propia | Medio | Revisión legal: Twilio como encargado |

## Derechos de los titulares

| Derecho | Cómo se atiende |
|---|---|
| Acceso | `GET /api/v1/conversations/{id}` (admin/operador) entrega transcripción y eventos de la conversación |
| Supresión | `DELETE /api/v1/conversations/{id}` (solo `admin`): borra conversación, transcripciones, eventos y grabación (también el objeto en S3/local) y deja un registro `conversation.deleted` sin contenido |
| Rectificación | No aplica a transcripciones automáticas; se atiende borrando |
| Oposición / revocar grabación | La grabación solo ocurre con consentimiento por sesión; sin él no se graba |
| Portabilidad | Pendiente: exportación en formato estructurado (JSON del detalle de la conversación ya es utilizable) |

## Minimización y controles
- El audio no se persiste salvo grabación consentida; los argumentos de herramientas no se guardan.
- Los logs no incluyen audio, transcripciones ni argumentos; las credenciales se redactan.
- Aislamiento por organización con RLS forzada en la base de datos; el rol de la aplicación no es propietario.
- Pruebas y benchmarks usan solo datos sintéticos; el seed (`003`) es una organización «Demo Retail» ficticia.
- Copias de seguridad: contienen transcripciones. Rotan a 14 días (`BACKUP_KEEP_DAYS`); **una supresión no borra las copias ya hechas**, por lo que el plazo máximo real es la retención + 14 días. Debe constar en la política de privacidad.

## Pendiente de revisión legal
1. Contratos de encargo de tratamiento con OpenAI/Google/Twilio/proveedor de almacenamiento y evaluación de transferencia internacional.
2. Texto de la política de privacidad y del consentimiento de grabación mostrado en la interfaz.
3. Registro de actividades de tratamiento y evaluación de impacto si el cliente trata datos sensibles (p. ej. salud).
4. Plazos de retención por cliente y de copias de seguridad.
