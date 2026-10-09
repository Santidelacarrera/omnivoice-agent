"""Pruebas de contrato contra la API Realtime vigente de OpenAI.

Se omiten sin OPENAI_API_KEY (o con OPENAI_CONTRACT=0). Las ejecuta el workflow `contract` (manual y nocturno).
Verifican lo que el adaptador ASUME: que la API acepta nuestro `session.update`, y que los eventos que
mapeamos siguen llamándose igual. Si OpenAI cambia el esquema, fallan aquí y no en una llamada de un cliente.
Consumen unos centavos de crédito por ejecución.
"""
import asyncio
import json
import os

import pytest

from app.core.config import Settings
from app.realtime.provider import OpenAIRealtimeProvider

pytestmark = [
    pytest.mark.contract,
    pytest.mark.skipif(not os.getenv("OPENAI_API_KEY") or os.getenv("OPENAI_CONTRACT") == "0",
                       reason="requiere OPENAI_API_KEY con crédito"),
]

TIMEOUT = 30


def settings() -> Settings:
    return Settings(environment="development", persistence_backend="memory", state_backend="memory",
                    openai_api_key=os.environ["OPENAI_API_KEY"])


async def raw_events(p: OpenAIRealtimeProvider, until: set[str], timeout: float = TIMEOUT) -> list[dict]:
    seen: list[dict] = []

    async def read():
        async for raw in p.ws:
            m = json.loads(raw)
            seen.append(m)
            if m.get("type") in until or m.get("type") == "error":
                return

    await asyncio.wait_for(read(), timeout)
    return seen


async def test_session_update_is_accepted_without_error():
    p = OpenAIRealtimeProvider(settings())
    await p.connect("Eres un asistente de pruebas. Responde muy breve.", [], voice="alloy", language="es")
    try:
        evs = await raw_events(p, {"session.updated"})
        errors = [e for e in evs if e.get("type") == "error"]
        assert not errors, f"OpenAI rechazó nuestro session.update: {errors[0]}"
        assert evs[-1]["type"] == "session.updated"
    finally:
        await p.close()


async def test_text_turn_produces_audio_transcript_and_done_with_mapped_event_names():
    p = OpenAIRealtimeProvider(settings())
    await p.connect("Responde solo: hola.", [], voice="alloy", language="es")
    try:
        await raw_events(p, {"session.updated"})
        await p._send({"type": "conversation.item.create", "item": {
            "type": "message", "role": "user", "content": [{"type": "input_text", "text": "Saluda."}]}})
        await p._send({"type": "response.create"})
        evs = await raw_events(p, {"response.done"})
        types = {e["type"] for e in evs}
        assert "response.done" in types
        # Los nombres que el adaptador mapea deben seguir existiendo (versión GA o beta).
        assert types & {"response.audio.delta", "response.output_audio.delta"}, sorted(types)
        assert types & {"response.audio_transcript.done", "response.output_audio_transcript.done"}, sorted(types)
    finally:
        await p.close()


async def test_adapter_events_normalization_end_to_end():
    p = OpenAIRealtimeProvider(settings())
    await p.connect("Responde solo: hola.", [], voice="alloy", language="es")
    try:
        await p._send({"type": "conversation.item.create", "item": {
            "type": "message", "role": "user", "content": [{"type": "input_text", "text": "Saluda."}]}})
        await p._send({"type": "response.create"})
        kinds: set[str] = set()

        async def read():
            async for ev in p.events():
                kinds.add(ev["type"])
                assert ev["type"] != "error", ev
                if ev["type"] == "response_done":
                    return

        await asyncio.wait_for(read(), TIMEOUT)
        assert {"audio_delta", "response_done"} <= kinds
    finally:
        await p.close()


async def test_all_catalog_voices_are_accepted():
    s = settings()
    bad = []
    for voice in s.allowed_voices:
        p = OpenAIRealtimeProvider(s)
        await p.connect("Prueba.", [], voice=voice, language="es")
        try:
            evs = await raw_events(p, {"session.updated"})
            if any(e.get("type") == "error" for e in evs):
                bad.append(voice)
        finally:
            await p.close()
    assert not bad, f"voces del catálogo que la API rechaza: {bad}"
