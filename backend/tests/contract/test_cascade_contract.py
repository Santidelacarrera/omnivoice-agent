"""Contrato con los servicios reales del pipeline en cascada (Deepgram STT/TTS + Anthropic).
Se omite sin DEEPGRAM_API_KEY y ANTHROPIC_API_KEY. La prueba estrella es el círculo completo: el TTS dice una frase,
esa audio entra por el STT y el LLM responde con voz — sin ningún dato de prueba grabado."""
import asyncio
import os

import pytest

from app.core.config import Settings
from app.realtime.cascade import AnthropicLLM, CascadedProvider, DeepgramSTT, DeepgramTTS

pytestmark = [pytest.mark.contract,
              pytest.mark.skipif(not (os.getenv("DEEPGRAM_API_KEY") and os.getenv("ANTHROPIC_API_KEY")),
                                 reason="requiere DEEPGRAM_API_KEY y ANTHROPIC_API_KEY")]


def settings() -> Settings:
    return Settings(environment="development", deepgram_api_key=os.environ["DEEPGRAM_API_KEY"],
                    anthropic_api_key=os.environ["ANTHROPIC_API_KEY"])


async def test_llm_streams_text_and_reports_usage():
    llm = AnthropicLLM(settings())
    try:
        evs = [e async for e in llm.stream("Responde en una frase corta.", [{"role": "user", "content": "Hola"}], [], 60)]
    finally:
        await llm.close()
    assert any(e["type"] == "text" and e["text"] for e in evs)
    usage = [e for e in evs if e["type"] == "usage"]
    assert usage and usage[-1]["in"] > 0 and usage[-1]["out"] > 0


async def test_tts_returns_pcm16_at_platform_rate():
    tts = DeepgramTTS(settings())
    try:
        pcm = b"".join([c async for c in tts.synth("Hola, ¿qué tal?", None, "es")])
    finally:
        await tts.close()
    assert len(pcm) > 24000  # > 0,5 s de audio a 24 kHz PCM16 mono


async def test_full_turn_with_real_services_and_stage_timings():
    s = settings()
    tts_user = DeepgramTTS(s)
    try:
        speech = b"".join([c async for c in tts_user.synth("Hola, ¿cuál es la política de devoluciones?", None, "es")])
    finally:
        await tts_user.close()
    p = CascadedProvider(s, DeepgramSTT(s), AnthropicLLM(s), DeepgramTTS(s))
    await p.connect("Eres un agente de atención al cliente. Responde en una frase.", [], language="es")
    try:
        frame = 960
        for i in range(0, len(speech), frame):
            await p.send_audio(speech[i:i + frame].ljust(frame, b"\x00"))
            await asyncio.sleep(0.02)
        for _ in range(60):  # 1,2 s de silencio para que el STT cierre el turno
            await p.send_audio(b"\x00" * frame)
            await asyncio.sleep(0.02)
        seen: dict[str, object] = {}

        async def collect():
            async for ev in p.events():
                seen.setdefault(ev["type"], ev)
                if ev["type"] == "stage":
                    seen[f"stage:{ev['stage']}"] = ev
                if ev["type"] in ("response_done", "error"):
                    return

        await asyncio.wait_for(collect(), 30)
        assert "error" not in seen, seen.get("error")
        assert "transcript_user" in seen and "devoluci" in seen["transcript_user"]["text"].lower()
        assert "audio_delta" in seen and {"stage:llm", "stage:tts"} <= set(seen)
    finally:
        await p.close()
