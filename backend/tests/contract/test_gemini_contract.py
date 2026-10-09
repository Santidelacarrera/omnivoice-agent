"""Contrato contra Gemini Live: la API acepta nuestro `setup` y devuelve `setupComplete`.
Se omite sin GEMINI_API_KEY (la clave gratuita de AI Studio sirve)."""
import os

import pytest

from app.core.config import Settings
from app.realtime.gemini import GeminiLiveProvider

pytestmark = [pytest.mark.contract,
              pytest.mark.skipif(not os.getenv("GEMINI_API_KEY"), reason="requiere GEMINI_API_KEY")]


async def test_setup_is_accepted_and_returns_audio_for_text_turn():
    s = Settings(environment="development", gemini_api_key=os.environ["GEMINI_API_KEY"])
    p = GeminiLiveProvider(s)
    await p.connect("Responde en una frase.", [], voice="Kore", language="es")  # lanza si no hay setupComplete
    try:
        await p._send({"realtimeInput": {"text": "Hola"}})
        got_audio = False
        async for ev in p.events():
            if ev["type"] == "audio_delta":
                got_audio = True
            if ev["type"] in ("response_done", "error"):
                assert ev["type"] != "error", ev
                break
        assert got_audio
    finally:
        await p.close()
