"""Adaptador de Gemini Live API (Google) con la misma interfaz que el de OpenAI.

Protocolo (WebSocket bidireccional): primer mensaje `setup`; audio de entrada PCM16 a 16 kHz en
`realtimeInput.audio`; audio de salida PCM16 a 24 kHz en `serverContent.modelTurn.parts[].inlineData`;
herramientas con `toolCall` / `toolResponse`. Documentación: https://ai.google.dev/api/live
Se valida con `tests/contract/test_gemini_contract.py` (necesita GEMINI_API_KEY).
"""
import asyncio
import base64
import json
from typing import Any, AsyncIterator

import structlog
import websockets

from app.core.config import Settings
from app.realtime.vad import resample_pcm16

log = structlog.get_logger()

GEMINI_URL = "wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
INPUT_RATE = 16000
OUTPUT_RATE = 24000
_DROP_KEYS = {"title", "additionalProperties", "$schema", "default", "examples"}


def to_gemini_schema(node: Any) -> Any:
    """JSON Schema de pydantic → subconjunto OpenAPI que admite Gemini (sin title/additionalProperties/default)."""
    if isinstance(node, dict):
        return {k: to_gemini_schema(v) for k, v in node.items() if k not in _DROP_KEYS}
    if isinstance(node, list):
        return [to_gemini_schema(v) for v in node]
    return node


def to_function_declarations(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    decls = []
    for t in tools:
        d: dict[str, Any] = {"name": t["name"], "description": t.get("description", "")}
        params = to_gemini_schema(t.get("parameters") or {})
        if params.get("properties"):  # Gemini rechaza objetos sin propiedades
            d["parameters"] = params
        decls.append(d)
    return decls


class GeminiLiveProvider:
    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self.ws: websockets.WebSocketClientProtocol | None = None
        self._call_names: dict[str, str] = {}
        self._user_text: list[str] = []
        self._agent_text: list[str] = []

    async def connect(self, instructions: str, tools: list[dict[str, Any]], voice: str | None = None,
                      language: str | None = None) -> None:
        # La clave viaja en la query (así lo define la API); no se escribe en logs.
        self.ws = await websockets.connect(f"{GEMINI_URL}?key={self.s.gemini_api_key}", max_size=2**22)
        generation: dict[str, Any] = {"responseModalities": ["AUDIO"]}
        if voice and voice in self.s.gemini_voices:
            generation["speechConfig"] = {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}}
        setup: dict[str, Any] = {
            "model": f"models/{self.s.gemini_live_model}",
            "generationConfig": generation,
            "systemInstruction": {"parts": [{"text": instructions}]},
            "inputAudioTranscription": {},
            "outputAudioTranscription": {},
        }
        decls = to_function_declarations(tools)
        if decls:
            setup["tools"] = [{"functionDeclarations": decls}]
        await self._send({"setup": setup})
        first = json.loads(await asyncio.wait_for(self.ws.recv(), timeout=15))
        if "setupComplete" not in first:  # error de configuración: se propaga con el motivo
            raise RuntimeError(f"gemini_setup_failed: {str(first)[:300]}")

    async def _send(self, msg: dict[str, Any]) -> None:
        assert self.ws is not None
        await self.ws.send(json.dumps(msg))

    async def send_audio(self, pcm: bytes) -> None:
        data = base64.b64encode(resample_pcm16(pcm, self.s.sample_rate, INPUT_RATE)).decode()
        await self._send({"realtimeInput": {"audio": {"data": data, "mimeType": f"audio/pcm;rate={INPUT_RATE}"}}})

    async def send_tool_result(self, call_id: str, output: dict[str, Any]) -> None:
        name = self._call_names.pop(call_id, "")
        await self._send({"toolResponse": {"functionResponses": [{"id": call_id, "name": name, "response": output}]}})

    async def cancel_response(self) -> None:
        # Gemini corta su respuesta solo cuando oye al usuario (detección de actividad del servidor);
        # no hay mensaje de cancelación. El orquestador ya descarta el audio obsoleto por época.
        return None

    def _flush(self, buf: list[str]) -> str:
        text = "".join(buf).strip()
        buf.clear()
        return text

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        assert self.ws is not None
        async for raw in self.ws:
            m = json.loads(raw)
            sc = m.get("serverContent")
            if sc:
                if (it := sc.get("inputTranscription")) and it.get("text"):
                    self._user_text.append(it["text"])
                for part in (sc.get("modelTurn") or {}).get("parts", []):
                    inline = part.get("inlineData")
                    if inline and inline.get("data"):
                        if text := self._flush(self._user_text):
                            yield {"type": "transcript_user", "text": text}
                        yield {"type": "audio_delta", "audio": base64.b64decode(inline["data"])}
                if (ot := sc.get("outputTranscription")) and ot.get("text"):
                    self._agent_text.append(ot["text"])
                if sc.get("interrupted"):
                    self._agent_text.clear()
                if sc.get("turnComplete"):
                    if text := self._flush(self._user_text):
                        yield {"type": "transcript_user", "text": text}
                    if text := self._flush(self._agent_text):
                        yield {"type": "transcript_agent", "text": text}
                    yield {"type": "response_done"}
            for fc in (m.get("toolCall") or {}).get("functionCalls", []):
                self._call_names[fc["id"]] = fc["name"]
                yield {"type": "tool_call", "call_id": fc["id"], "name": fc["name"],
                       "arguments": json.dumps(fc.get("args") or {})}
            if "error" in m:
                yield {"type": "error", "message": str(m["error"].get("message", "provider_error"))[:300]}

    async def close(self) -> None:
        if self.ws:
            await self.ws.close()
