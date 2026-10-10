# Latencia extremo a extremo bajo carga

Generado por `bench.loadtest` + `bench.report`. Cada tabla procede de una ejecución real de esas herramientas; el modo (real/simulado) figura siempre debajo del título.

## A. 1 conversación, red ideal — 1 clientes, 20 s (2026-10-10T18:31:12Z)

_Proveedor SIMULADO: mide solo la plataforma (WebSocket, VAD, orquestación). No es la latencia que percibe un usuario._

Condiciones: proveedor activo: `cascade` · semilla 1

| Métrica | muestras | p50 ms | p95 ms | p99 ms |
|---|---|---|---|---|
| Primer audio tras fin de turno (total) | 14 | 825.7 | 936.5 | 936.5 |
| Barge-in (ida y vuelta) | 14 | 0.8 | 1 | 1 |
| Etapa STT (último instante de voz → transcripción final; incluye el endpointing) | 14 | 431.1 | 489.8 | 489.8 |
| Etapa LLM (transcripción → primera frase lista) | 14 | 249.5 | 348.5 | 348.5 |
| Etapa TTS (primera frase → primer audio) | 14 | 153.1 | 207.7 | 207.7 |

Conectadas: 1 · rechazadas por cupo: 0 · timeouts: 0 · errores: 0
Coste estimado (consumo medido × precios configurados): **0.0653 USD/min** sobre 0.34 min · desglose: stt 0.0029, llm 0.0073, tts 0.0121

## B. 50 conversaciones simultáneas, red ideal — 50 clientes, 20 s (2026-10-10T18:31:37Z)

_Proveedor SIMULADO: mide solo la plataforma (WebSocket, VAD, orquestación). No es la latencia que percibe un usuario._

Condiciones: proveedor activo: `cascade` · semilla 1

| Métrica | muestras | p50 ms | p95 ms | p99 ms |
|---|---|---|---|---|
| Primer audio tras fin de turno (total) | 699 | 835.9 | 977.4 | 1057 |
| Barge-in (ida y vuelta) | 699 | 1.5 | 6.7 | 10.2 |
| Etapa STT (último instante de voz → transcripción final; incluye el endpointing) | 699 | 432.8 | 487.5 | 517.6 |
| Etapa LLM (transcripción → primera frase lista) | 699 | 251.5 | 376.7 | 442.9 |
| Etapa TTS (primera frase → primer audio) | 699 | 151.2 | 228 | 258.7 |

Conectadas: 50 · rechazadas por cupo: 0 · timeouts: 0 · errores: 0
Coste estimado (consumo medido × precios configurados): **0.0636 USD/min** sobre 17.43 min · desglose: stt 0.1435, llm 0.3670, tts 0.5974

## C. 50 conversaciones, red lenta (RTT 200 ms ± 40) — 50 clientes, 20 s (2026-10-10T18:32:02Z)

_Proveedor SIMULADO: mide solo la plataforma (WebSocket, VAD, orquestación). No es la latencia que percibe un usuario._

Condiciones: proveedor activo: `cascade` · red del cliente simulada: RTT 200 ms ± 40 ms · semilla 1

| Métrica | muestras | p50 ms | p95 ms | p99 ms |
|---|---|---|---|---|
| Primer audio tras fin de turno (total) | 550 | 1041.5 | 1198.2 | 1254.4 |
| Barge-in (ida y vuelta) | 550 | 205 | 261.5 | 273.8 |
| Etapa STT (último instante de voz → transcripción final; incluye el endpointing) | 550 | 432.3 | 510 | 543.7 |
| Etapa LLM (transcripción → primera frase lista) | 550 | 246.2 | 372.1 | 420.7 |
| Etapa TTS (primera frase → primer audio) | 550 | 150 | 225.6 | 259.8 |

Conectadas: 50 · rechazadas por cupo: 0 · timeouts: 0 · errores: 0
Coste estimado (consumo medido × precios configurados): **0.0594 USD/min** sobre 17.32 min · desglose: stt 0.1129, llm 0.2888, tts 0.6270

## D. 200 conversaciones simultáneas, red ideal — 200 clientes, 20 s (2026-10-10T18:32:36Z)

_Proveedor SIMULADO: mide solo la plataforma (WebSocket, VAD, orquestación). No es la latencia que percibe un usuario._

Condiciones: proveedor activo: `cascade` · semilla 1

| Métrica | muestras | p50 ms | p95 ms | p99 ms |
|---|---|---|---|---|
| Primer audio tras fin de turno (total) | 2627 | 864.4 | 1060.6 | 1156.2 |
| Barge-in (ida y vuelta) | 2627 | 23.5 | 140.9 | 184.4 |
| Etapa STT (último instante de voz → transcripción final; incluye el endpointing) | 2627 | 436.7 | 519.5 | 562.2 |
| Etapa LLM (transcripción → primera frase lista) | 2627 | 264.9 | 395 | 458.8 |
| Etapa TTS (primera frase → primer audio) | 2627 | 159.4 | 237.7 | 278.3 |

Conectadas: 200 · rechazadas por cupo: 0 · timeouts: 0 · errores: 0
Coste estimado (consumo medido × precios configurados): **0.0604 USD/min** sobre 68.42 min · desglose: stt 0.5394, llm 1.3792, tts 2.2112

