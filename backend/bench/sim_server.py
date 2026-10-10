"""Servidor de medición con un pipeline en cascada SIMULADO (sin claves ni red) y latencias configurables.

Ejercita el código real del producto —WebSocket, VAD, orquestación, `CascadedProvider`, métricas por etapa,
barge-in, límites— sustituyendo solo STT/LLM/TTS por clientes que esperan lo que se les indica. Sirve para:
  * validar la metodología (que stt+llm+tts+endpointing ≈ primer audio medido) con una verdad conocida,
  * medir el coste de la plataforma bajo concurrencia y conexiones lentas.
NO mide a ningún proveedor: las cifras que salgan de aquí deben publicarse como «simulado».

Uso:  python -m bench.sim_server --port 8011 --stt-ms 120 --llm-ms 250 --tts-ms 150 --jitter 0.25 --seed 1
Luego: python -m bench.loadtest --url http://localhost:8011 --mode simulated --clients 50 --duration 20
"""
import argparse
import asyncio
import os
import random
from typing import Any, AsyncIterator

from app.core.config import Settings
from app.realtime.cascade import CascadedProvider
from app.realtime.vad import rms_pcm16


class SimSTT:
    """Emite una transcripción `stt_ms` después de que el usuario calla (más el endpointing, como un STT real)."""

    def __init__(self, delay, endpointing_ms: float):
        self.delay, self.endpointing = delay, endpointing_ms / 1000
        self.q: asyncio.Queue[str | None] = asyncio.Queue()
        self._voiced = False
        self._silence = 0.0
        self._n = 0

    async def start(self, language): ...

    async def send(self, pcm: bytes) -> None:
        loud = rms_pcm16(pcm) > 0.01
        if loud:
            self._voiced, self._silence = True, 0.0
        elif self._voiced:
            self._silence += len(pcm) / 2 / 24000
            if self._silence >= self.endpointing:
                self._voiced = False
                self._n += 1
                asyncio.get_running_loop().call_later(self.delay(), self.q.put_nowait, f"turno simulado {self._n}")

    async def utterances(self) -> AsyncIterator[str]:
        while (t := await self.q.get()) is not None:
            yield t

    async def close(self) -> None:
        await self.q.put(None)


class SimLLM:
    def __init__(self, delay, use_tool: bool = False):
        self.delay, self.use_tool = delay, use_tool

    async def stream(self, system, messages, tools, max_tokens) -> AsyncIterator[dict[str, Any]]:
        await asyncio.sleep(self.delay())
        last = messages[-1]["content"]
        if self.use_tool and isinstance(last, str):  # primera pasada del turno: pide una herramienta real del registro
            yield {"type": "text", "text": "Déjame comprobarlo. "}
            yield {"type": "tool_use", "id": "sim-tool", "name": "check_inventory",
                   "input": {"product": "chaqueta", "color": "negro", "size": "M"}}
            return
        yield {"type": "text", "text": "Entendido, te ayudo con eso. "}
        yield {"type": "text", "text": "¿Algo más?"}
        yield {"type": "usage", "in": 400, "out": 25}

    async def close(self) -> None: ...


class SimTTS:
    def __init__(self, delay):
        self.delay = delay

    async def synth(self, text, voice, language) -> AsyncIterator[bytes]:
        await asyncio.sleep(self.delay())
        for _ in range(25):  # 0,5 s de audio por frase, entregado en bloques
            yield b"\x01\x00" * 480
            await asyncio.sleep(0.005)

    async def close(self) -> None: ...


def build(args) -> Any:
    from app.main import create_app

    rng = random.Random(args.seed)

    def lat(ms: float):
        # lognormal con mediana `ms`: cola larga realista; jitter = sigma
        return lambda: max(0.0, ms / 1000 * rng.lognormvariate(0, args.jitter)) if args.jitter else ms / 1000

    settings = Settings(environment="development", persistence_backend="memory", state_backend="memory",
                        jwt_secret="x" * 40, vad_backend="energy", max_sessions_per_org=10_000,
                        rate_limit_sessions_per_min=10**6, rate_limit_api_per_min=10**7, max_total_sessions=10_000,
                        retention_job_enabled=False, deepgram_api_key="sim", anthropic_api_key="sim",
                        vad_hangover_ms=args.vad_hangover_ms)
    return create_app(settings, provider_factory=lambda: CascadedProvider(
        settings, SimSTT(lat(args.stt_ms), args.endpointing_ms), SimLLM(lat(args.llm_ms), args.tool), SimTTS(lat(args.tts_ms))))


if __name__ == "__main__":
    import uvicorn

    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8011)
    ap.add_argument("--stt-ms", type=float, default=120, help="mediana de la espera del STT tras el endpointing")
    ap.add_argument("--endpointing-ms", type=float, default=300)
    ap.add_argument("--llm-ms", type=float, default=250, help="mediana hasta la primera frase")
    ap.add_argument("--tts-ms", type=float, default=150, help="mediana hasta el primer audio")
    ap.add_argument("--jitter", type=float, default=0.0, help="sigma de la lognormal (0 = determinista)")
    ap.add_argument("--vad-hangover-ms", type=int, default=600)
    ap.add_argument("--tool", action="store_true", help="el LLM simulado pide check_inventory en cada turno (demo de herramientas)")
    ap.add_argument("--seed", type=int, default=1)
    a = ap.parse_args()
    os.environ.setdefault("LOG_LEVEL", "WARNING")
    uvicorn.run(build(a), host="127.0.0.1", port=a.port, log_level="warning")
