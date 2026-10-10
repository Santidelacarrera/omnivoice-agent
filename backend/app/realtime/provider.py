"""Abstracción del proveedor de voz en tiempo real + adaptador OpenAI Realtime + proveedor simulado.

Eventos normalizados que emite un proveedor (dict con "type"):
  audio_delta {audio: bytes}, transcript_user {text}, transcript_agent {text},
  tool_call {call_id, name, arguments}, response_done {}, error {message}
"""
import asyncio
import base64
import json
from typing import Any, AsyncIterator, Protocol

import websockets

from app.core.config import Settings


class RealtimeProvider(Protocol):
    async def connect(self, instructions: str, tools: list[dict[str, Any]], voice: str | None = None,
                      language: str | None = None) -> None: ...
    async def send_audio(self, pcm: bytes) -> None: ...
    async def send_tool_result(self, call_id: str, output: dict[str, Any]) -> None: ...
    async def cancel_response(self) -> None: ...
    def events(self) -> AsyncIterator[dict[str, Any]]: ...
    async def close(self) -> None: ...


class OpenAIRealtimeProvider:
    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self.ws: websockets.WebSocketClientProtocol | None = None

    async def connect(self, instructions: str, tools: list[dict[str, Any]], voice: str | None = None,
                      language: str | None = None) -> None:
        url = f"{self.s.openai_realtime_url}?model={self.s.openai_realtime_model}"
        self.ws = await websockets.connect(
            url, extra_headers={"Authorization": f"Bearer {self.s.openai_api_key}"}, max_size=2**22
        )
        transcription: dict[str, Any] = {"model": "whisper-1"}
        if language:
            transcription["language"] = language.split("-")[0]  # ISO-639-1: mejora la transcripción
        session: dict[str, Any] = {
            "instructions": instructions,
            "tools": tools,
            "input_audio_format": "pcm16",
            "output_audio_format": "pcm16",
            "turn_detection": {"type": "server_vad", "create_response": True, "interrupt_response": True},
            "input_audio_transcription": transcription,
        }
        if voice:
            session["voice"] = voice
        await self._send({"type": "session.update", "session": session})

    async def _send(self, msg: dict[str, Any]) -> None:
        assert self.ws is not None
        await self.ws.send(json.dumps(msg))

    async def send_audio(self, pcm: bytes) -> None:
        await self._send({"type": "input_audio_buffer.append", "audio": base64.b64encode(pcm).decode()})

    async def send_tool_result(self, call_id: str, output: dict[str, Any]) -> None:
        await self._send(
            {
                "type": "conversation.item.create",
                "item": {"type": "function_call_output", "call_id": call_id, "output": json.dumps(output)},
            }
        )
        await self._send({"type": "response.create"})

    async def cancel_response(self) -> None:
        await self._send({"type": "response.cancel"})

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        assert self.ws is not None
        async for raw in self.ws:
            m = json.loads(raw)
            t = m.get("type", "")
            if t == "response.audio.delta" or t == "response.output_audio.delta":
                yield {"type": "audio_delta", "audio": base64.b64decode(m["delta"])}
            elif t == "conversation.item.input_audio_transcription.completed":
                yield {"type": "transcript_user", "text": m.get("transcript", "")}
            elif t in ("response.audio_transcript.done", "response.output_audio_transcript.done"):
                yield {"type": "transcript_agent", "text": m.get("transcript", "")}
            elif t == "response.function_call_arguments.done":
                yield {"type": "tool_call", "call_id": m["call_id"], "name": m["name"], "arguments": m["arguments"]}
            elif t == "response.done":
                yield {"type": "response_done"}
            elif t == "error":
                yield {"type": "error", "message": m.get("error", {}).get("message", "provider_error")}

    async def close(self) -> None:
        if self.ws:
            await self.ws.close()


class FakeProvider:
    """Proveedor determinista para desarrollo sin clave y para tests."""

    def __init__(self, script: list[dict[str, Any]] | None = None, audio_chunks: int = 20, chunk_delay: float = 0.02,
                 respond_on_audio: bool = False):
        self.respond_on_audio = respond_on_audio  # responde al primer audio recibido (tests de API/WS)
        self._q: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self.script = script or []
        self.audio_chunks = audio_chunks
        self.chunk_delay = chunk_delay
        self.cancelled = 0
        self.received_audio = 0
        self.tool_results: list[tuple[str, dict[str, Any]]] = []
        self._task: asyncio.Task | None = None

    async def connect(self, instructions, tools, voice=None, language=None) -> None:
        self.voice, self.language = voice, language

    async def _respond(self) -> None:
        for ev in self.script:
            await self._q.put(ev)
        for _ in range(self.audio_chunks):
            await asyncio.sleep(self.chunk_delay)
            await self._q.put({"type": "audio_delta", "audio": b"\x01\x00" * 480})
        await self._q.put({"type": "response_done"})

    def trigger_response(self) -> None:
        self._task = asyncio.create_task(self._respond())

    async def send_audio(self, pcm: bytes) -> None:
        self.received_audio += len(pcm)
        if self.respond_on_audio and self._task is None:
            self.trigger_response()

    async def send_tool_result(self, call_id, output) -> None:
        self.tool_results.append((call_id, output))
        await self._q.put({"type": "audio_delta", "audio": b"\x02\x00" * 480})
        await self._q.put({"type": "response_done"})

    async def cancel_response(self) -> None:
        self.cancelled += 1
        if self._task and not self._task.done():
            self._task.cancel()

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        while True:
            ev = await self._q.get()
            if ev is None:
                return
            yield ev

    async def close(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()  # sin tareas huérfanas tras cerrar
        await self._q.put(None)
