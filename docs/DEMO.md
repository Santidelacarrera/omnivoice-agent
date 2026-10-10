# Demo reproducible

El objetivo de la demo es enseñar, con evidencias observables: **conversación natural**, **interrupción a media respuesta**, **llamada a
herramienta con resultado** y **métricas con metodología reproducible**.

> **Estado.** La grabación con voz y proveedores reales **no está hecha**: requiere tus claves (`DEEPGRAM_API_KEY` + `ANTHROPIC_API_KEY`, o
> `GEMINI_API_KEY`/`OPENAI_API_KEY`) y un micrófono. Todo lo demás está listo: el guion automático, la captura de evidencias y el
> informe. Abajo hay una ejecución del guion contra el pipeline **simulado** para que se vea el formato exacto de lo que producirá.

## Opción 1 — en el navegador (la grabación de pantalla)
1. `cp .env.example .env`, añade las claves y `docker compose up --build`.
2. Abre `http://localhost:4310/conversation`, elige voz e idioma y pulsa iniciar.
3. Guion sugerido (graba pantalla + audio del sistema):
   1. «Hola, ¿tenéis stock de la chaqueta negra talla M?» → el agente llama a `check_inventory`; la pantalla muestra la herramienta y su **resultado**.
   2. Mientras el agente contesta, **interrúmpelo**: «Perdona, mejor dime qué política de devoluciones tenéis». El audio se corta al instante y responde a lo nuevo.
   3. Provoca un fallo controlado: detén el proveedor o corta la red unos segundos → aviso «Reconectando con el proveedor de voz…» y la conversación sigue.
4. El panel muestra «Primer audio», «Silencio tras interrupción» y, con la cascada, la línea «Última respuesta por etapas (STT/LLM/TTS)».

## Opción 2 — guion automático con evidencias (`bench/demo.py`)
```bash
cd backend
python -m bench.demo --url http://localhost:8010 \
    --say1 "Hola, ¿tenéis stock de la chaqueta negra talla M?" \
    --say2 "Perdona, mejor dime qué política de devoluciones tenéis" \
    --interrupt-after-ms 700 --out demo-out/
```
Genera `demo-out/timeline.json` (todos los eventos con su marca de tiempo) y `demo-out/agent.wav` (el audio que el agente llegó a emitir; termina
donde se le interrumpió). Con `--wav1/--wav2` usas tus propias grabaciones en lugar de la voz sintetizada. Si usas VAD de modelo, la voz debe ser real o de un TTS.

Ejecución del guion contra el pipeline **simulado** (`bench.sim_server --tool`; transcripciones y audio no son reales):
```
      0 ms  user_turn_1
   1427 ms  transcript_user    text=turno simulado 1
   1929 ms  metrics            first_audio_ms=930 stt=428.4 llm=300.6 tts=200.6
   1929 ms  first_audio        ms_after_last_voice=910
   2062 ms  tool.start         name=check_inventory
   2062 ms  tool.end           name=check_inventory ok=True result={"available": true, "units": 4, "price": 89.9}
   2436 ms  user_interrupts    agent_frames_so_far=25
   2437 ms  audio.clear (barge-in)
   3861 ms  transcript_user    text=turno simulado 2
```
Lo que demuestra: el desglose por etapas suma el primer audio (428+301+201 ≈ 930 ms), la herramienta se ejecuta contra datos reales del registro y
el audio se vacía ~1 ms después de la interrupción (medido en el servidor; el cliente añade la red).

## Qué publicar con la demo
- El vídeo/pantalla de la opción 1 y los ficheros de la opción 2.
- `docs/LATENCY.md` regenerado con una ejecución `--mode real` (ver su sección 4) y `docs/COSTS.md` con el coste/min leído de `/api/v1/metrics`.
- La versión (commit), proveedores y modelos usados.
