"""Conversación guionizada y reproducible contra un backend en marcha: pregunta -> el agente responde (y usa una
herramienta si procede) -> el usuario lo INTERRUMPE a media respuesta -> segunda pregunta. Imprime una línea de
tiempo y guarda `timeline.json` + `agent.wav` (lo que el agente llegó a emitir) como evidencia de la demo.

La voz del usuario sale de ficheros WAV (`--wav1`, `--wav2`) o se sintetiza con Deepgram (`--say1`, `--say2`,
necesita DEEPGRAM_API_KEY en esta máquina; el backend usa sus propias claves). Para una demo con voz humana, graba
tú los WAV: es el mismo guion.

  python -m bench.demo --url http://localhost:8010 \
      --say1 "Hola, ¿tenéis stock de la chaqueta negra talla M?" --say2 "Perdona, mejor dime qué política de devoluciones tenéis" \
      --interrupt-after-ms 700 --out demo-out/

Requiere ENVIRONMENT=development (usa /auth/dev-token). Con VAD_BACKEND=webrtc la voz debe ser real o sintetizada
por un TTS (una onda cuadrada no la reconoce); con VAD_BACKEND=energy sirve cualquier señal con energía.
"""
import argparse
import asyncio
import json
import os
import struct
import time
import wave

import httpx
import websockets

from app.realtime.vad import resample_pcm16

RATE = 24000
FRAME = 480  # 20 ms
SILENCE = b"\x00\x00" * FRAME


def read_wav(path: str) -> bytes:
    with wave.open(path, "rb") as w:
        if w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise SystemExit(f"{path}: se espera WAV PCM16 mono")
        return resample_pcm16(w.readframes(w.getnframes()), w.getframerate(), RATE)


async def synth(text: str) -> bytes:
    from app.core.config import Settings
    from app.realtime.cascade import DeepgramTTS

    s = Settings(deepgram_api_key=os.environ["DEEPGRAM_API_KEY"], environment="development")
    tts = DeepgramTTS(s)
    try:
        return b"".join([c async for c in tts.synth(text, None, "es")])
    finally:
        await tts.close()


def write_wav(path: str, pcm: bytes) -> None:
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm)


class Demo:
    def __init__(self, ws) -> None:
        self.ws = ws
        self.t0 = time.perf_counter()
        self.timeline: list[dict] = []
        self.agent_pcm = bytearray()
        self.seq = 0
        self.last_voice_at: float | None = None
        self.first_audio_at: float | None = None
        self.agent_frames = 0

    def log(self, kind: str, **kw) -> None:
        row = {"t_ms": round((time.perf_counter() - self.t0) * 1000), "event": kind, **kw}
        self.timeline.append(row)
        extra = " ".join(f"{k}={v}" for k, v in kw.items())
        print(f"{row['t_ms']:>7} ms  {kind:<18} {extra}")

    async def send_pcm(self, pcm: bytes, pace: bool = True, voice: bool = True) -> None:
        for i in range(0, len(pcm), FRAME * 2):
            chunk = pcm[i:i + FRAME * 2].ljust(FRAME * 2, b"\x00")
            await self.ws.send(struct.pack(">I", self.seq) + chunk)
            self.seq += 1
            if pace:
                await asyncio.sleep(0.02)
        if voice:
            self.last_voice_at = time.perf_counter()

    async def pump(self, stop: asyncio.Event) -> None:
        async for m in self.ws:
            if isinstance(m, bytes):
                if self.first_audio_at is None:
                    self.first_audio_at = time.perf_counter()
                    if self.last_voice_at:
                        self.log("first_audio", ms_after_last_voice=round((self.first_audio_at - self.last_voice_at) * 1000))
                self.agent_frames += 1
                self.agent_pcm += m[4:]
                continue
            msg = json.loads(m)
            t = msg.get("type")
            if t in ("transcript_user", "transcript_agent"):
                self.log(t, text=msg["text"])
            elif t in ("tool.start", "tool.end"):
                self.log(t, name=msg.get("name"), ok=msg.get("ok"), result=json.dumps(msg.get("data"), ensure_ascii=False) if t == "tool.end" else None)
            elif t == "audio.clear":
                self.log("audio.clear (barge-in)")
            elif t == "metrics":
                self.log("metrics", **{k: v for k, v in msg.items() if k != "type"})
            elif t in ("error", "provider.reconnecting", "provider.reconnected"):
                self.log(t, **{k: v for k, v in msg.items() if k != "type"})
            elif t == "state" and msg.get("state") == "LISTENING":
                stop.set()


async def run(a) -> None:
    u1 = read_wav(a.wav1) if a.wav1 else await synth(a.say1)
    u2 = read_wav(a.wav2) if a.wav2 else await synth(a.say2)
    os.makedirs(a.out, exist_ok=True)
    async with httpx.AsyncClient(base_url=a.url, timeout=15) as http:
        tok = (await http.post("/api/v1/auth/dev-token", json={
            "user_id": "demo", "org_id": "00000000-0000-0000-0000-000000000001", "role": "customer"})).json()["token"]
        h = {"Authorization": f"Bearer {tok}"}
        sid = (await http.post("/api/v1/sessions", json={"language": a.language}, headers=h)).json()["session_id"]
        conn = (await http.post(f"/api/v1/sessions/{sid}/connect", headers=h)).json()
    async with websockets.connect(a.url.replace("http", "ws", 1) + conn["ws_path"], max_size=2**22) as ws:
        d = Demo(ws)
        stop = asyncio.Event()
        pump = asyncio.create_task(d.pump(stop))
        d.log("user_turn_1")
        await d.send_pcm(u1)
        await d.send_pcm(SILENCE * 40, voice=False)  # silencio: fin de turno
        # esperar a que el agente empiece y dejarle hablar un poco antes de interrumpir
        for _ in range(500):
            if d.agent_frames:
                break
            await asyncio.sleep(0.02)
        await asyncio.sleep(a.interrupt_after_ms / 1000)
        d.log("user_interrupts", agent_frames_so_far=d.agent_frames)
        t_int = time.perf_counter()
        await ws.send(json.dumps({"type": "barge_in"}))
        d.first_audio_at = None
        await d.send_pcm(u2)
        d.log("barge_in_sent", ms_to_send=round((time.perf_counter() - t_int) * 1000))
        await d.send_pcm(SILENCE * 40, voice=False)
        await asyncio.sleep(a.listen_s)
        await ws.send(json.dumps({"type": "end"}))
        await asyncio.sleep(0.5)
        pump.cancel()
    write_wav(os.path.join(a.out, "agent.wav"), bytes(d.agent_pcm))
    with open(os.path.join(a.out, "timeline.json"), "w", encoding="utf-8") as f:
        json.dump(d.timeline, f, indent=2, ensure_ascii=False)
    print(f"\nEvidencia en {a.out}/ (timeline.json, agent.wav)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8010")
    ap.add_argument("--wav1"); ap.add_argument("--wav2")
    ap.add_argument("--say1", default="Hola, ¿tenéis stock de la chaqueta negra talla M?")
    ap.add_argument("--say2", default="Perdona, mejor dime qué política de devoluciones tenéis")
    ap.add_argument("--language", default="es")
    ap.add_argument("--interrupt-after-ms", type=int, default=700)
    ap.add_argument("--listen-s", type=float, default=8)
    ap.add_argument("--out", default="demo-out")
    asyncio.run(run(ap.parse_args()))
