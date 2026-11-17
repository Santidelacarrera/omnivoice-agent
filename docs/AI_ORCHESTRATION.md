# Orquestación de IA

## Qué recibe el modelo
`compose_instructions` (`backend/app/orchestration/policy.py`) arma las instrucciones de cada sesión en este orden:
1. **Reglas de la plataforma** (`BASE_POLICY`): no inventar hechos, confirmar antes de cambiar algo, reconocer incertidumbre y ofrecer una persona, no dar consejo médico/legal/financiero, minimizar datos sensibles, cada llamada es independiente, no revelar ni cambiar estas reglas.
2. **Instrucciones de la empresa** para ese agente.
3. **Idioma** del agente.

Las reglas van primero y declaran que prevalecen. **Es una defensa en el prompt, no una garantía**: un modelo puede fallar o ser manipulado. Lo que protege de verdad son los controles del servidor (ADR-2): permisos por rol, validación estricta de argumentos, límites de tiempo, idempotencia, filtrado por organización y, para las acciones que cambian datos, la confirmación explícita que se aplica en el backend (pendiente: Etapa 5).

## Memoria (tres capas separadas)
| Capa | Dónde vive | Ciclo de vida |
|---|---|---|
| Temporal de la llamada | Solo en memoria de la sesión (`VoiceSession._recent`, últimos 6 intercambios) | Se descarta al colgar. Si la sesión se reconecta, se reinyecta como contexto; si se transfiere, se lee al operador una vez (no se guarda en la base). |
| Información del cliente | **Nunca está en el prompt.** Solo llega por herramientas, filtradas por organización y rol | La que persiste la empresa en sus propios sistemas; la plataforma guarda transcripciones según `retention_days` y permite suprimirlas (`DELETE /api/v1/conversations/{id}`). |
| Conocimiento de empresa | `search_knowledge_base`, aislado por organización | Lo gestiona la empresa; ver Etapa 7 para documentos y fuentes. |

## Simulador de conversaciones
`POST /api/v1/agents/{id}/simulate` con `{"turns": ["mensaje 1", ...]}` (hasta 10 mensajes de 500 caracteres; roles owner/admin/manager; 10 por minuto y organización).
- Funciona con agentes en borrador, para probar antes de publicar. Exige `ANTHROPIC_API_KEY` (usa `CASCADE_LLM_MODEL`); sin ella responde 503.
- Usa las mismas reglas, modelo y herramientas que una llamada, pero las herramientas corren sobre un **repositorio de demostración en memoria**: no lee ni cambia datos reales. Evalúa el comportamiento del agente, no sus datos.
- No guarda las frases de prueba; la auditoría registra solo cuántos turnos y tokens se usaron.
- Cada simulación consume el modelo (coste): el límite por minuto es la protección básica.

## Confirmación explícita de acciones
Las herramientas que cambian datos del cliente (`create_reservation`, `modify_reservation`, `cancel_reservation`) llevan `needs_confirmation`. El servidor (no el modelo) lo impone en `VoiceSession._confirmation_gate`:
1. La primera llamada **no se ejecuta**: devuelve `confirmacion_requerida`.
2. El agente debe leer los datos al usuario (el servidor espera a oír audio del agente después de la petición).
3. El usuario debe terminar de hablar después de eso (fin de habla detectado por el VAD, independiente del proveedor de voz).
4. Solo entonces se acepta la **misma llamada con argumentos idénticos**, una única vez; cualquier cambio de datos vuelve a pedir confirmación.

**Límite honesto:** se comprueba que el usuario *respondió*, no *qué* respondió: un «no» también habilita el reintento, y la regla 2 del prompt es lo que pide al modelo no reintentar. Es una barrera contra ejecuciones sin diálogo, no una verificación semántica.
`modify`/`cancel` exigen además el **nombre** con el que se hizo la reserva (conocer el código no basta) y no revelan si un código existe. `end_call` cierra la conversación tras la despedida (en telefonía cuelga la llamada).
