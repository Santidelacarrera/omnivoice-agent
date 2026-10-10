# Proveedores y costes de ejecución

## Proveedores de voz soportados

| Modo (`VOICE_PROVIDER`) | Servicios | Variables | Etapas medibles por separado | Verificado contra el servicio real |
|---|---|---|---|---|
| `gemini` | Gemini Live (voz-a-voz) | `GEMINI_API_KEY`, `GEMINI_LIVE_MODEL` | No (un solo servicio) | Contrato en `tests/contract/test_gemini_contract.py` (se ejecuta con tu clave) |
| `openai` | OpenAI Realtime (voz-a-voz) | `OPENAI_API_KEY` | No | Contrato en `tests/contract/test_openai_contract.py` |
| `cascade` | **Deepgram** STT (`nova-3`) + **Anthropic** LLM (`CASCADE_LLM_MODEL`) + **Deepgram** TTS (Aura-2) | `DEEPGRAM_API_KEY`, `ANTHROPIC_API_KEY` | **Sí**: STT / LLM / TTS | Contrato en `tests/contract/test_cascade_contract.py` (círculo completo TTS→STT→LLM→TTS). Los clientes HTTP/WS están probados contra servidores locales, **no** contra los servicios reales hasta que ejecutes el contrato con tus claves |
| *(sin clave)* | Proveedor simulado | — | — | Solo para ver la interfaz |

`auto` elige Gemini → OpenAI → cascada → simulado según las claves presentes. Las claves viven solo en el backend; el navegador nunca las ve.

## Cómo se calcula el coste

`coste = consumo MEDIDO × precio CONFIGURADO`. El consumo (segundos de audio, tokens, caracteres) lo mide la plataforma en cada sesión;
los precios son configuración (`PRICE_*`) porque cambian y varían por plan. **Los valores por defecto son referencias, no hechos verificados:
confírmalos en la página de precios de cada proveedor antes de tomar decisiones o facturar.**

| Componente | Consumo medido | Precio por defecto (`Settings`) |
|---|---|---|
| STT (cascada) | segundos de audio enviados al STT | `price_stt_per_min` = 0,0077 USD/min |
| LLM (cascada) | tokens de entrada/salida (incluye caché) | `price_llm_in_per_mtok` = 1,0 · `price_llm_out_per_mtok` = 5,0 USD/Mtok |
| TTS (cascada) | caracteres enviados al TTS | `price_tts_per_1k_chars` = 0,030 USD |
| Voz-a-voz | segundos de audio entrante / saliente | `price_s2s_in_per_min` = 0,06 · `price_s2s_out_per_min` = 0,24 USD/min |

No incluye red, cómputo propio, PostgreSQL, Redis ni almacenamiento de grabaciones.

Dónde ver el coste real de tus conversaciones:
- Por sesión: `cost_usd` en el registro de auditoría `session.completed` y en el resumen de la conversación.
- Agregado por proceso: `GET /api/v1/metrics` → `cost` (`cost_per_minute_usd`, desglose por componente).
- Series: `omnivoice_conversation_cost_usd_total{component}` / `omnivoice_conversation_seconds_total` en Prometheus →
  coste/min = `rate(omnivoice_conversation_cost_usd_total[1h]) / (rate(omnivoice_conversation_seconds_total[1h]) / 60)`.

**Coste por minuto: no hay todavía cifra real publicada.** Los informes de `docs/LATENCY.md` calculan coste con tokens y caracteres *simulados*
(400 tokens de entrada y 25 de salida por turno, frases fijas), así que su «USD/min» no representa ningún servicio. Para obtener el tuyo:
ejecuta `bench.loadtest --mode real` (o una demo larga) con tus claves y lee `cost` del informe, tras verificar los precios.

## Controles de consumo (todos verificados por tests)

| Control | Variable | Efecto |
|---|---|---|
| Duración máxima de sesión | `MAX_SESSION_SECONDS` (1800) | Cierra el WebSocket con 4408 |
| Concurrencia por organización | `MAX_SESSIONS_PER_ORG` (50) | 429 al crear sesión / 4429 al conectar (cupo atómico en Redis) |
| Concurrencia global por proceso | `MAX_TOTAL_SESSIONS` (200) | 429 / 4429; métrica `omnivoice_budget_rejected_total{reason="global_concurrency"}` |
| Presupuesto diario por organización | `ORG_DAILY_BUDGET_MINUTES` (0 = sin tope) | Sin presupuesto: 429 y 4429; además la sesión en curso se acota al presupuesto restante. El consumo se carga al cerrar (Redis, día UTC) |
| Rate limit | `RATE_LIMIT_SESSIONS_PER_MIN`, `RATE_LIMIT_API_PER_MIN` | 429 |
| Plazo de conexión al proveedor | `PROVIDER_CONNECT_TIMEOUT_S` (10) | La sesión no se queda colgada si el proveedor no responde |
| Reconexión acotada | `PROVIDER_MAX_RECONNECTS` (3 por sesión) | Tras agotarla, la sesión termina con `provider_unavailable`: un proveedor caído no genera reintentos infinitos |
| Historial del LLM | `CASCADE_HISTORY_MESSAGES` (20), `CASCADE_MAX_TOKENS` (300), `CASCADE_MAX_TOOL_ROUNDS` (4) | Acota tokens por turno |
| Duración de un turno | `CASCADE_TURN_TIMEOUT_S` (30) | Un turno colgado se aborta y se informa |

Limitación: el presupuesto diario se descuenta al **cerrar** la sesión; entre medias, el consumo máximo está acotado por
`MAX_SESSION_SECONDS × concurrencia`. Un proceso que muera sin cerrar la sesión no carga ese consumo.
