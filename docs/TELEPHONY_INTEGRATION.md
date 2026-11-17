# Integración telefónica

## Ruta elegida
**Proveedor con streaming de audio hacia el runtime propio** (Twilio Media Streams). La alternativa evaluada, un trunk SIP directo contra el motor de voz en tiempo real, no se implementó: sus condiciones y capacidades no se han verificado con el proveedor y no hay ninguna necesidad actual que Twilio no cubra. Se reevaluará si el coste por minuto o los requisitos de un cliente lo justifican.

## Flujo de una llamada entrante
1. Twilio llama a `POST /telephony/voice` (firma `X-Twilio-Signature` validada).
2. El número marcado (`To`) se busca en `phone_numbers` (función `resolve_phone_number`): fija la **organización** y el **agente**. Solo contesta un agente `published`; borrador, archivado o sin agente → aviso y colgado, con `telephony.call_rejected` en la auditoría. Un número sin asignar usa `TELEPHONY_ORG_ID`/`TELEPHONY_AGENT_ID` salvo que `TELEPHONY_ALLOW_UNMAPPED=false`.
3. Se aplican límites (peticiones por llamante, sesiones por organización), se registra la llamada (`calls`, estado CREATED) y se responde con TwiML `<Connect><Stream>` con un ticket de un solo uso.
4. Twilio abre `WS /ws/telephony`; la sesión pasa a CONNECTED y el audio fluye (μ-law 8 kHz ↔ PCM 24 kHz).
5. Al colgar, COMPLETED. Una segunda entrega del webhook para una llamada ya conectada o terminada se rechaza (`telephony.call_duplicate`).

## Estados
CREATED, QUEUED, RINGING, ANSWERED, CONNECTED, COMPLETED, FAILED, BUSY, NO_ANSWER, CANCELLED, TRANSFERRED. Solo avanzan (`backend/app/telephony/call_state.py`); un estado final no cambia. Los avisos de estado de Twilio llegan a `POST /telephony/status` (firmados, idempotentes por llamada y evento). **En números entrantes hay que configurar el *Status Callback* en Twilio**; en las salientes lo pide la plataforma.

## Transferencia a un operador
La inicia el agente (`transfer_to_human`) o una persona (`POST /api/v1/calls/{id}/transfer`). El destino es el `transfer_number` de la línea o, si no tiene, `HUMAN_TRANSFER_NUMBER`. Al contestar el operador, Twilio pide `POST /telephony/whisper?h=<token>` y le lee, solo a él, el motivo y los últimos intercambios. Ese contexto vive 5 minutos en Redis, de un solo uso, y **no se guarda en la base de datos**. Quien tiene `phones:write` decide a qué número se desvía: restríngelo.

## Llamadas salientes
`POST /api/v1/outbound-calls` crea un trabajo con base de consentimiento obligatoria, ventana horaria en la zona del destinatario, reintentos y lista de exclusión; ver `docs/RUNBOOK.md`.

## Resiliencia
Idempotencia de eventos por `(proveedor, CallSid)` y por evento; timeouts; reintentos controlados de las salientes; recuperación de trabajos "marcando" abandonados; errores del proveedor registrados y contados (`omnivoice_errors_total`, `omnivoice_call_events_total`). Pendiente: alertas de disponibilidad del proveedor y gestión de llamadas abandonadas.

## Contrato con el proveedor
`TelephonyProvider` (`backend/app/telephony/provider.py`) cubre solo las **órdenes salientes** (marcar, desviar, colgar). La entrada (webhooks, TwiML, firma) sigue siendo de Twilio: un segundo proveedor necesita además su propio adaptador de entrada que traduzca sus eventos a los estados anteriores.
