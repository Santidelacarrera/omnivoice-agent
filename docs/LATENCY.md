# Latencia: metodología, resultados y objetivos

> **Estado honesto.** Las cifras de este documento son de un **pipeline en cascada simulado** (`bench/sim_server.py`): ejecutan el
> código real de la plataforma (WebSocket, VAD, orquestación, `CascadedProvider`, métricas, barge-in) pero con STT/LLM/TTS
> sustituidos por esperas conocidas. Validan la **metodología** y miden el coste de **nuestra** plataforma; **no son la latencia de
> ningún proveedor real**. Las mediciones con proveedores reales se obtienen con el mismo procedimiento (sección 4) y todavía
> no están publicadas.

## 1. Qué se mide (definiciones)

| Término | Definición exacta |
|---|---|
| **Fin de turno** | Instante del **último chunk con voz** del usuario (`SessionMetrics.last_voice_at`). No es el «fin de habla» que declara el VAD: ese llega `vad_hangover_ms` (600 ms) después y mediría de menos. Si el proveedor responde antes de que el VAD declare el fin, la muestra se registra igualmente. |
| **Primer audio** (`first_audio_ms`) | Fin de turno → primer frame PCM de la respuesta enviado al cliente. Es lo que percibe la persona, **incluida** la espera de endpointing del STT. |
| **Etapa STT** | Fin de turno → transcripción final del turno disponible (incluye el endpointing, p. ej. 300 ms). |
| **Etapa LLM** | Transcripción final → primera **frase** completa del LLM (es lo que el TTS necesita para empezar). |
| **Etapa TTS** | Primera frase entregada al TTS → primer byte de audio. |
| **Barge-in (servidor)** | Detección de voz durante la respuesta → `audio.clear` emitido (`omnivoice_barge_in_seconds`). |
| **Barge-in (ida y vuelta)** | Medido por el cliente de carga: `barge_in` enviado → `audio.clear` recibido (incluye la red). |

`primer audio ≈ STT + LLM + TTS` (+ pocos ms de la plataforma). Con proveedores voz-a-voz (OpenAI Realtime, Gemini Live) las etapas
**no son separables** —es un único servicio— y solo se publica el total; la separación en tres etapas requiere el proveedor `cascade`.

Dónde se registra: histogramas Prometheus `omnivoice_first_audio_seconds`, `omnivoice_stage_seconds{stage}` y
`omnivoice_barge_in_seconds`; ventanas por proceso con p50/p95/p99 en `GET /api/v1/metrics` (`first_audio_ms`, `stage_ms`, `barge_in_ms`);
el cliente recibe el desglose de cada turno en el mensaje `metrics` (`first_audio_ms`, `stt`, `llm`, `tts`).

**Percentiles:** nearest-rank sobre muestras individuales (`ceil(q·n)`); ventana por proceso de 2000 muestras. Con menos de ~100 muestras
el p99 es solo el máximo: no lo cites. Entre réplicas, agrega los histogramas de Prometheus, no los percentiles.

## 2. Instrumento de carga

`bench/loadtest.py` abre N conversaciones reales (HTTP + WebSocket), y en cada una repite: 0,6 s de habla → 0,8 s de silencio →
espera el primer frame de audio → interrumpe (`barge_in` + habla) → espera `audio.clear`. Mide con relojes del cliente.
- **Conexión lenta:** `--rtt-ms 200 --jitter-ms 40` retrasa cada mensaje en ambos sentidos (RTT/2 ± jitter, sin reordenar).
- **Concurrencia:** `--clients N` conversaciones simultáneas; los rechazos por cupo (429) se cuentan aparte.
- **Reproducibilidad:** `--seed` fija el jitter; `sim_server --seed` fija las latencias simuladas (lognormal, σ=0,25).
- La habla sintética es una señal de energía: el servidor de medición usa `VAD_BACKEND=energy`. El VAD de modelo (WebRTC) debe evaluarse con voz real (`bench/demo.py`).

## 3. Validación de la metodología (verdad conocida)

El servidor simulado se configura con medianas: endpointing 300 ms + STT 120 ms + LLM 250 ms + TTS 150 ms = **820 ms**. Con 1 conversación y red ideal
(escenario A) se mide **826 ms** de primer audio, con etapas STT 431 (=300+120+overhead), LLM 250, TTS 153 ms. El desglose reproduce la entrada
configurada con < 1 % de error, y la suma de etapas coincide con el total medido de forma independiente por el cliente.

## 4. Cómo reproducir

```bash
cd backend
pip install -r requirements.txt
bash bench/run_matrix.sh ../docs/bench-results     # escenarios A–D simulados → report.md + JSON por escenario
python -m bench.check_slo ../docs/slo.json ../docs/bench-results/*.json   # compara con los objetivos

# Con proveedores reales (necesitas tus claves en .env y el stack arrancado, ENVIRONMENT=development, VAD_BACKEND=energy):
python -m bench.loadtest --url http://localhost:8010 --clients 10 --duration 60 --mode real \
    --label "B. cascada real, 10 conversaciones" --seed 1 --out r.json
python -m bench.report r.json                       # tabla con p50/p95/p99, etapas y coste/min
```
Anota siempre: proveedor y modelo, región/red de la máquina que genera carga, `--rtt-ms`, número de clientes, duración, versión (commit).
Orden de magnitud: una respuesta real tarda varios segundos, así que 10 clientes durante 60 s dan del orden de 100 muestras por métrica (válido para p50; para p95/p99 alarga la duración o sube los clientes hasta tener ≥ 500). El escenario A (14 muestras) solo sirve para p50.
Los resultados contienen `meta.provider_mode`; `bench.report` imprime siempre si era real o simulado.

## 5. Resultados (SIMULADOS — plataforma, no proveedores)

Ejecutados con `bash bench/run_matrix.sh` en un contenedor de desarrollo (generador de carga y servidor en la misma máquina). Datos crudos en `docs/bench-results/*.json`.

### A. 1 conversación, red ideal — 1 clientes, 20 s (2026-10-10T18:31:12Z)

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

### B. 50 conversaciones simultáneas, red ideal — 50 clientes, 20 s (2026-10-10T18:31:37Z)

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

### C. 50 conversaciones, red lenta (RTT 200 ms ± 40) — 50 clientes, 20 s (2026-10-10T18:32:02Z)

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

### D. 200 conversaciones simultáneas, red ideal — 200 clientes, 20 s (2026-10-10T18:32:36Z)

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


### Lectura de los resultados
- **Plataforma bajo concurrencia (A→B):** pasar de 1 a 50 conversaciones apenas cambia el primer audio (p50 826 → 836 ms) y el barge-in sigue en milisegundos (p95 6,7 ms). El coste añadido por nuestro código es pequeño frente a cualquier proveedor real.
- **Red lenta (B→C):** el primer audio sube ≈ 205 ms (p50 836 → 1042 ms) y el barge-in medido por el cliente pasa a ≈ 205 ms: el RTT simulado de 200 ms se suma una vez, como es de esperar. Por eso el corte **local** de audio del navegador (nivel 1 del barge-in) es el que da sensación de inmediatez; el round-trip al servidor no puede ser menor que la red.
- **Límite de la medición en D (200 conversaciones):** generador y servidor comparten CPU, así que el p95 de barge-in (141 ms) y el p50 (23 ms) mezclan la contención del propio generador con la del servidor. Sirve para ver que no hay errores ni timeouts a 200 conversaciones, no para dimensionar: dimensiona con un generador en otra máquina.
- El coste/min que imprime el informe usa tokens y caracteres **simulados**; no es un coste real (ver `docs/COSTS.md`).

## 6. Objetivos de latencia

Los objetivos viven en [`slo.json`](slo.json) y se verifican con `bench.check_slo` (sale con código 1 si se incumplen; un objetivo sin muestras cuenta como incumplido).

| Ámbito | Estado | Origen del valor |
|---|---|---|
| Plataforma (simulado, escenarios A–D) | **Basado en medición** | Valor medido + margen (~25 %): p95 primer audio ≤ 1250 ms con 50 conversaciones y las latencias simuladas; barge-in p95 ≤ 15 ms (red ideal) / ≤ 330 ms con RTT 200 ms. Si el código de la plataforma empeora, `check_slo` lo detecta. |
| Proveedor real (primer audio p50 ≤ 1500 ms, p95 ≤ 3000 ms; barge-in p95 ≤ 300 ms) | **Provisional, no medido** | Hipótesis de partida. **Regla para fijarlos:** tras la primera ejecución real, objetivo = p95 medido + 15 % y se sustituye el valor en `slo.json`. Hasta entonces no deben citarse como logro. |

Los objetivos históricos del README (p50 < 500 ms) **no se sostienen** para un pipeline en cascada: solo el endpointing del STT consume 300 ms. Alcanzarlos requiere un proveedor voz-a-voz o reducir `endpointing`/`utterance_end`, a costa de cortar al usuario en pausas.

## 7. Límites conocidos
- No hay todavía medición con proveedores reales (requiere claves y crédito); los contratos están en `tests/contract/test_cascade_contract.py`.
- La voz del generador de carga es sintética: valida tiempos de plataforma, no la calidad del VAD ni del STT con ruido.
- Las ventanas de `/api/v1/metrics` son por proceso. Con varias réplicas usa Prometheus.
