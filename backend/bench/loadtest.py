"""Prueba de carga extremo a extremo contra un servidor en marcha (HTTP + WebSocket reales).

Cada cliente virtual: crea sesión -> connect -> abre WS -> envía habla sintética (20 ms/frame) ->
mide tiempo hasta el primer frame de audio de respuesta -> interrumpe y mide hasta 'audio.clear'.
Imprime p50/p95/p99 y cuántas sesiones fueron rechazadas (429) para estimar capacidad concurrente.

Con OPENAI_API_KEY vacío el backend usa el proveedor simulado: sirve para medir tu stack, no al proveedor.
Uso:  python -m bench.loadtest --url http://localhost:8000 --clients 50 --duration 20
Requiere el backend con ENVIRONMENT=development (usa /auth/dev-token).
"""
import argparse
import asyncio
import json
import math
import struct
import time

import httpx
import websockets

SPEECH = struct.pack("<480h", *([12000, -12000] * 240))  # 20 ms a 24 kHz, energía alta
SILENCE = b"\x00\x00" * 480


def pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    v = sorted(values)
    return round(v[min(len(v) - 1, max(0, math.ceil(q * len(v)) - 1))] * 1000, 1)


async def client(i: int, base: str, duration: float, first_audio: list[float], barge: list[float], stats: dict) -> None:
    async with httpx.AsyncClient(base_url=base, timeout=10) as http:
        tok = (await http.post("/api/v1/auth/dev-token", json={"user_id": f"load{i}", "org_id": "00000000-0000-0000-0000-000000000001", "role": "customer"})).json()["token"]
        h = {"Authorization": f"Bearer {tok}"}
        r = await http.post("/api/v1/sessions", json={}, headers=h)
        if r.status_code == 429:
            stats["rejected"] += 1
            return
        sid = r.json()["session_id"]
        conn = (await http.post(f"/api/v1/sessions/{sid}/connect", headers=h)).json()
    ws_url = base.replace("http", "ws", 1) + conn["ws_path"]
    try:
        async with websockets.connect(ws_url, max_size=2**22) as ws:
            await ws.recv()  # session.ready
            stats["connected"] += 1
            end = time.perf_counter() + duration
            seq = 0
            while time.perf_counter() < end:
                # turno: 0.6 s de habla, 0.8 s de silencio (fin de turno), esperar audio, interrumpir
                for _ in range(30):
                    await ws.send(struct.pack(">I", seq) + SPEECH); seq += 1
                    await asyncio.sleep(0.02)
                t_end = time.perf_counter()
                for _ in range(40):
                    await ws.send(struct.pack(">I", seq) + SILENCE); seq += 1
                    await asyncio.sleep(0.02)
                got = False
                try:
                    while True:
                        m = await asyncio.wait_for(ws.recv(), timeout=2)
                        if isinstance(m, bytes) and not got:
                            first_audio.append(time.perf_counter() - t_end)
                            got = True
                            t_b = time.perf_counter()
                            await ws.send(json.dumps({"type": "barge_in"}))
                        elif isinstance(m, str) and json.loads(m).get("type") == "audio.clear":
                            barge.append(time.perf_counter() - t_b)
                            break
                except asyncio.TimeoutError:
                    stats["timeouts"] += 1
            await ws.send(json.dumps({"type": "end"}))
    except Exception:  # noqa: BLE001
        stats["errors"] += 1


async def main(base: str, clients: int, duration: float) -> None:
    first_audio: list[float] = []
    barge: list[float] = []
    stats = {"connected": 0, "rejected": 0, "timeouts": 0, "errors": 0}
    await asyncio.gather(*(client(i, base, duration, first_audio, barge, stats) for i in range(clients)))
    print(json.dumps({
        "clients": clients, **stats,
        "first_audio_ms": {"count": len(first_audio), "p50": pct(first_audio, .5), "p95": pct(first_audio, .95), "p99": pct(first_audio, .99)},
        "barge_in_roundtrip_ms": {"count": len(barge), "p50": pct(barge, .5), "p95": pct(barge, .95), "p99": pct(barge, .99)},
    }, indent=2))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8000")
    ap.add_argument("--clients", type=int, default=25)
    ap.add_argument("--duration", type=float, default=15)
    a = ap.parse_args()
    asyncio.run(main(a.url, a.clients, a.duration))
