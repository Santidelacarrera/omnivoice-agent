"""Benchmark del orquestador SIN red ni proveedor real (proveedor simulado).

Mide cuánta latencia añade NUESTRO código bajo concurrencia:
  - barge-in: desde on_client_event('barge_in') hasta que 'audio.clear' sale hacia el cliente
  - fan-out: frames de audio entregados por segundo con N sesiones simultáneas

No mide la latencia del proveedor ni de la red: para eso usa bench/loadtest.py contra un servidor real.
Uso: python -m bench.orchestrator_bench --sessions 200 --rounds 5
"""
import argparse
import asyncio
import json
import time

from app.core.config import Settings
from app.observability.metrics import LatencyWindow
from app.orchestration.session import VoiceSession
from app.realtime.provider import FakeProvider
from app.security.auth import Principal
from app.services.persistence import InMemoryPersistence
from app.tools.handlers import InMemoryRepository, build_registry


async def one_session(idx: int, rounds: int, barge: LatencyWindow, frames: list[int], db, registry, settings) -> None:
    marks: dict[str, float] = {}
    count = 0

    async def send(m):
        nonlocal count
        if isinstance(m, (bytes, bytearray)):
            count += 1
        elif m.get("type") == "audio.clear":
            marks["clear"] = time.perf_counter()

    prov = FakeProvider(audio_chunks=200, chunk_delay=0.002)
    s = VoiceSession(Principal(f"u{idx}", f"org{idx % 5}", "customer"), prov, registry, send, settings, persistence=db)
    await s.start()
    for _ in range(rounds):
        marks.clear()
        prov._task = None
        prov.trigger_response()
        while count < 3:  # espera a que el agente esté hablando
            await asyncio.sleep(0.001)
        t0 = time.perf_counter()
        await s.on_client_event({"type": "barge_in"})
        barge.add(marks["clear"] - t0)
        count = 0
        await asyncio.sleep(0.01)
    frames.append(count)
    await s.close()


async def main(n: int, rounds: int) -> dict:
    settings = Settings(environment="test", sample_rate=24000, vad_backend="energy")
    registry = build_registry(InMemoryRepository())
    db = InMemoryPersistence()
    barge, frames = LatencyWindow(size=n * rounds + 10), []
    t0 = time.perf_counter()
    await asyncio.gather(*(one_session(i, rounds, barge, frames, db, registry, settings) for i in range(n)))
    wall = time.perf_counter() - t0
    p = barge.percentiles(scale=1_000_000)
    result = {
        "sessions": n, "rounds_per_session": rounds, "wall_seconds": round(wall, 2),
        "barge_in_server_handling_us": p,
        "note": "Solo orquestador (proveedor simulado, sin red). No es latencia extremo a extremo.",
    }
    print(json.dumps(result, indent=2))
    return result


def check_thresholds(result: dict, max_barge_p95_us: float | None, max_wall_s: float | None) -> list[str]:
    """Umbrales de regresión del orquestador. Sin muestras cuenta como incumplido: no se aprueba lo que no se midió."""
    failures = []
    p95 = result["barge_in_server_handling_us"].get("p95")
    if max_barge_p95_us is not None and (p95 is None or p95 > max_barge_p95_us):
        failures.append(f"barge-in p95 {p95} us > {max_barge_p95_us} us")
    if max_wall_s is not None and result["wall_seconds"] > max_wall_s:
        failures.append(f"tiempo total {result['wall_seconds']} s > {max_wall_s} s con {result['sessions']} sesiones")
    return failures


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--sessions", type=int, default=200)
    ap.add_argument("--rounds", type=int, default=5)
    ap.add_argument("--max-barge-p95-us", type=float, help="falla (exit 1) si el p95 de manejo del barge-in supera estos microsegundos")
    ap.add_argument("--max-wall-seconds", type=float, help="falla si el conjunto tarda más de estos segundos")
    a = ap.parse_args()
    res = asyncio.run(main(a.sessions, a.rounds))
    problems = check_thresholds(res, a.max_barge_p95_us, a.max_wall_seconds)
    for line in problems:
        print(f"UMBRAL INCUMPLIDO: {line}")
    raise SystemExit(1 if problems else 0)
