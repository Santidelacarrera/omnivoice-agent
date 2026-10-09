"""Adaptador Gemini Live: conversión de esquemas y mapeo de mensajes (sin red)."""
import base64
import json

import pytest

from app.core.config import Settings
from app.realtime.gemini import GeminiLiveProvider, to_function_declarations, to_gemini_schema


class FakeWS:
    def __init__(self, incoming):
        self.incoming, self.sent = incoming, []

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    def __aiter__(self):
        async def gen():
            for m in self.incoming:
                yield json.dumps(m)
        return gen()

    async def close(self): ...


def provider(incoming):
    p = GeminiLiveProvider(Settings(environment="test", gemini_api_key="k", sample_rate=24000))
    p.ws = FakeWS(incoming)
    return p


def test_schema_is_reduced_to_gemini_subset():
    schema = {"type": "object", "title": "X", "additionalProperties": False,
              "properties": {"q": {"type": "string", "title": "Q", "default": "a"}}, "required": ["q"]}
    assert to_gemini_schema(schema) == {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]}
    d = to_function_declarations([{"name": "a", "description": "d", "parameters": {"type": "object", "properties": {}}}])
    assert d == [{"name": "a", "description": "d"}]  # sin propiedades no se envían parameters


async def test_events_map_audio_transcripts_tools_and_turn_end():
    audio = base64.b64encode(b"\x01\x00" * 10).decode()
    p = provider([
        {"serverContent": {"inputTranscription": {"text": "hola "}}},
        {"serverContent": {"inputTranscription": {"text": "mundo"}}},
        {"serverContent": {"modelTurn": {"parts": [{"inlineData": {"data": audio, "mimeType": "audio/pcm;rate=24000"}}]}}},
        {"serverContent": {"outputTranscription": {"text": "Buenas"}}},
        {"toolCall": {"functionCalls": [{"id": "c1", "name": "check_inventory", "args": {"product": "x"}}]}},
        {"serverContent": {"turnComplete": True}},
    ])
    evs = [e async for e in p.events()]
    kinds = [e["type"] for e in evs]
    assert kinds == ["transcript_user", "audio_delta", "tool_call", "transcript_agent", "response_done"]
    assert evs[0]["text"] == "hola mundo" and evs[1]["audio"] == b"\x01\x00" * 10
    assert json.loads(evs[2]["arguments"]) == {"product": "x"}


async def test_tool_result_and_audio_input_format():
    p = provider([{"toolCall": {"functionCalls": [{"id": "c9", "name": "t", "args": {}}]}}])
    [e async for e in p.events()]
    await p.send_tool_result("c9", {"ok": True})
    assert p.ws.sent[-1] == {"toolResponse": {"functionResponses": [{"id": "c9", "name": "t", "response": {"ok": True}}]}}
    await p.send_audio(b"\x00\x00" * 480)  # 20 ms a 24 kHz → 320 muestras a 16 kHz
    a = p.ws.sent[-1]["realtimeInput"]["audio"]
    assert a["mimeType"] == "audio/pcm;rate=16000" and len(base64.b64decode(a["data"])) == 640


def test_provider_selection_and_voices():
    assert Settings(environment="test").active_provider == "simulated"
    s = Settings(environment="test", gemini_api_key="k")
    assert s.active_provider == "gemini" and "Kore" in s.available_voices
    assert Settings(environment="test", openai_api_key="k").available_voices[0] == "alloy"
