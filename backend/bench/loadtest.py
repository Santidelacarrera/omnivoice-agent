"""Prueba de carga extremo a extremo contra un servidor en marcha (HTTP + WebSocket reales).

Cada cliente virtual: crea sesión -> connect -> abre WS -> envía habla sintética (20 ms/frame) ->
mide tiempo hasta el primer frame de audio de respuesta -> interrumpe y mide hasta 'audio.clear'.
Imprime p50/p95/p99 y cuántas sesiones fueron rechazadas (429) para estimar capacidad concurrente.

La voz sintética es una onda cuadrada: un VAD de modelo (webrtc) no la toma por voz humana, así que
mide con VAD_BACKEND=energy en el servidor; el VAD de modelo se evalúa con audio real.
Con OPENAI_API_KEY vacío el backend usa el proveedor simulado: sirve para medir tu stack, no al proveedor.
Uso:  python -m bench.loadtest --url http://localhost:8000 --clients 50 --duration 20
Requiere el backend con ENVIRONMENT=development (usa /auth/dev-token).

Conexión lenta: `--rtt-ms 200 --jitter-ms 40` retrasa cada mensaje en ambos sentidos (rtt/2 ± jitter) preservando el
orden, como lo haría una red móvil mala. Las latencias medidas incluyen ese retardo, como las vería un usuario.
Etapas: con un proveedor en cascada el servidor informa stt/llm/tts en el mensaje `metrics`; se agregan aquí.
"""
import argparse
import asyncio
import json
import math
import random
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


class SlowWS:
    """Envoltorio de un WebSocket que añade retardo de red en ambos sentidos sin reordenar mensajes."""

    def __init__(self, ws, rtt_ms: float, jitter_ms: float) -> None:
        self.ws, self.d, self.j = ws, rtt_ms / 2000, jitter_ms / 1000
        self.out: asyncio.Queue = asyncio.Queue()
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.tasks = [asyncio.create_task(self._send_loop()), asyncio.create_task(self._recv_loop())]

    def _delay(self) -> float:
        return max(0.0, self.d + random.uniform(-self.j, self.j))

    async def _send_loop(self) -> None:
        last = 0.0
        while True:
            due, msg = await self.out.get()
            due = max(due, last)  # el orden se conserva
            last = due
            await asyncio.sleep(max(0.0, due - time.perf_counter()))
            await self.ws.send(msg)

    async def _recv_loop(self) -> None:
        last = 0.0
        try:
            async for msg in self.ws:
                due = max(time.perf_counter() + self._delay(), last)
                last = due
                await self.inbox.put((due, msg))
        finally:
            await self.inbox.put((0.0, None))

    async def send(self, msg) -> None:
        await self.out.put((time.perf_counter() + self._delay(), msg))

    async def recv(self):
        due, msg = await self.inbox.get()
        if msg is None:
            raise websockets.ConnectionClosed(None, None)
        await asyncio.sleep(max(0.0, due - time.perf_counter()))
        return msg

    async def close(self) -> None:
        await asyncio.sleep(self.d + self.j)  # deja salir los últimos mensajes ("end")
        for t in self.tasks:
            t.cancel()


async def client(i: int, base: str, duration: float, first_audio: list[float], barge: list[float], stats: dict,
                 stages: dict[str, list[float]] | None = None, rtt_ms: float = 0, jitter_ms: float = 0) -> None:
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
        async with websockets.connect(ws_url, max_size=2**22) as raw_ws:
            ws = SlowWS(raw_ws, rtt_ms, jitter_ms) if rtt_ms or jitter_ms else raw_ws
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
                            # El servidor confirma la interrupción con su propio VAD: hay que seguir "hablando".
                            for _ in range(10):
                                await ws.send(struct.pack(">I", seq) + SPEECH); seq += 1
                        elif isinstance(m, str):
                            msg = json.loads(m)
                            if msg.get("type") == "metrics" and stages is not None:
                                for k in ("stt", "llm", "tts"):
                                    if k in msg:
                                        stages[k].append(msg[k] / 1000)
                            elif msg.get("type") == "audio.clear":
                                barge.append(time.perf_counter() - t_b)
                                break
                except asyncio.TimeoutError:
                    stats["timeouts"] += 1
            await ws.send(json.dumps({"type": "end"}))
            if isinstance(ws, SlowWS):
                await ws.close()
    except Exception:  # noqa: BLE001
        stats["errors"] += 1


async def server_summary(base: str) -> dict:
    """Coste por minuto y proveedor activo según el propio servidor (necesita ENVIRONMENT=development)."""
    try:
        async with httpx.AsyncClient(base_url=base, timeout=10) as http:
            tok = (await http.post("/api/v1/auth/dev-token", json={
                "user_id": "bench-report", "org_id": "00000000-0000-0000-0000-000000000001", "role": "operator"})).json()["token"]
            m = (await http.get("/api/v1/metrics", headers={"Authorization": f"Bearer {tok}"})).json()
            return {"provider": m.get("provider"), "cost": m.get("cost")}
    except Exception:  # noqa: BLE001
        return {}


async def main(base: str, clients: int, duration: float, out: str | None = None, mode: str = "unspecified",
               label: str = "", rtt_ms: float = 0, jitter_ms: float = 0, seed: int | None = None) -> None:
    if seed is not None:
        random.seed(seed)
    first_audio: list[float] = []
    barge: list[float] = []
    stages: dict[str, list[float]] = {"stt": [], "llm": [], "tts": []}
    stats = {"connected": 0, "rejected": 0, "timeouts": 0, "errors": 0}
    await asyncio.gather(*(client(i, base, duration, first_audio, barge, stats, stages, rtt_ms, jitter_ms)
                           for i in range(clients)))
    summary = await server_summary(base)
    result = {
        "meta": {"timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "url": base, "duration_s": duration,
                 "provider_mode": mode, "label": label, "rtt_ms": rtt_ms, "jitter_ms": jitter_ms, "seed": seed,
                 "server_provider": summary.get("provider")},
        "clients": clients, **stats,
        "stages_ms": {k: {"count": len(v), "p50": pct(v, .5), "p95": pct(v, .95), "p99": pct(v, .99)}
                      for k, v in stages.items()},
        "cost": summary.get("cost"),
        "first_audio_ms": {"count": len(first_audio), "p50": pct(first_audio, .5), "p95": pct(first_audio, .95), "p99": pct(first_audio, .99)},
        "barge_in_roundtrip_ms": {"count": len(barge), "p50": pct(barge, .5), "p95": pct(barge, .95), "p99": pct(barge, .99)},
    }
    print(json.dumps(result, indent=2))
    if out:
        with open(out, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8000")
    ap.add_argument("--clients", type=int, default=25)
    ap.add_argument("--duration", type=float, default=15)
    ap.add_argument("--out", help="guarda el resultado en JSON (entrada de bench.report)")
    ap.add_argument("--mode", choices=["real", "simulated", "unspecified"], default="unspecified",
                    help="real = OPENAI_API_KEY con crédito; simulated = proveedor falso (mide solo tu stack)")
    ap.add_argument("--rtt-ms", type=float, default=0, help="retardo de ida y vuelta simulado de la red del cliente")
    ap.add_argument("--jitter-ms", type=float, default=0, help="variación aleatoria por mensaje (±)")
    ap.add_argument("--seed", type=int, help="semilla del jitter, para repetir exactamente la medición")
    ap.add_argument("--label", default="", help="p. ej. 'VM 2 vCPU, VAD_BACKEND=energy'")
    a = ap.parse_args()
    asyncio.run(main(a.url, a.clients, a.duration, a.out, a.mode, a.label, a.rtt_ms, a.jitter_ms, a.seed))
