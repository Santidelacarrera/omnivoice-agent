"""Proveedor en cascada: STT en streaming -> LLM en streaming (con herramientas) -> TTS en streaming.

Implementa el mismo contrato que los proveedores voz-a-voz (`RealtimeProvider`), pero al ser tres servicios
distintos permite **medir cada etapa por separado**. Emite, además de los eventos normales, eventos `stage`:

  {"type": "stage", "stage": "stt_final", "t": <monotonic>}   la transcripción final llegó (la sesión calcula
                                                               stt = t - último instante de voz)
  {"type": "stage", "stage": "llm", "ms": ...}                transcripción -> primera frase lista para sintetizar
  {"type": "stage", "stage": "tts", "ms": ...}                primera frase -> primer audio

Las implementaciones concretas (Deepgram, Anthropic) están detrás de protocolos mínimos para poder probar la
orquestación sin red. Las pruebas de contrato con los servicios reales viven en `tests/contract`.
"""
import asyncio
import json
import re
import time
from typing import Any, AsyncIterator, Protocol

import httpx
import structlog
import websockets

from app.core.config import Settings

log = structlog.get_logger()

FRAME_BYTES = 960  # 20 ms de PCM16 a 24 kHz mono
_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+|\n+")


class STTClient(Protocol):
    async def start(self, language: str | None) -> None: ...
    async def send(self, pcm: bytes) -> None: ...
    def utterances(self) -> AsyncIterator[str]: ...  # un texto por turno del usuario terminado
    async def close(self) -> None: ...


class LLMClient(Protocol):
    # Eventos: {"type":"text","text"} · {"type":"tool_use","id","name","input"} · {"type":"usage","in","out"}
    def stream(self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]],
               max_tokens: int) -> AsyncIterator[dict[str, Any]]: ...
    async def close(self) -> None: ...


class TTSClient(Protocol):
    def synth(self, text: str, voice: str | None, language: str | None) -> AsyncIterator[bytes]: ...
    async def close(self) -> None: ...


def split_sentences(buf: str, final: bool = False, min_clause: int = 40) -> tuple[list[str], str]:
    """Separa `buf` en frases completas y un resto. El resto se libera antes en una coma si ya es largo,
    para que el TTS empiece sin esperar al final de una frase larga."""
    parts = _SENTENCE_END.split(buf)
    if len(parts) == 1 and not final:
        if len(buf) >= min_clause and "," in buf[min_clause // 2:]:
            cut = buf.rfind(",") + 1
            return [buf[:cut].strip()], buf[cut:].lstrip()
        return [], buf
    done = [p.strip() for p in parts[:-1] if p.strip()]
    rest = parts[-1]
    if final:
        if rest.strip():
            done.append(rest.strip())
        rest = ""
    return done, rest


class CascadedProvider:
    def __init__(self, settings: Settings, stt: STTClient, llm: LLMClient, tts: TTSClient) -> None:
        self.s = settings
        self.stt, self.llm, self.tts = stt, llm, tts
        self.usage = {"llm_in_tokens": 0, "llm_out_tokens": 0, "tts_chars": 0, "stt_seconds": 0.0}
        self._q: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._system = ""
        self._tools: list[dict[str, Any]] = []
        self._voice: str | None = None
        self._language: str | None = None
        self._history: list[dict[str, Any]] = []
        self._turn: asyncio.Task | None = None
        self._pump: asyncio.Task | None = None
        self._tool_waiters: dict[str, asyncio.Future] = {}
        self._closed = False

    # ---- contrato RealtimeProvider ----
    async def connect(self, instructions: str, tools: list[dict[str, Any]], voice: str | None = None,
                      language: str | None = None) -> None:
        self._system = instructions
        self._tools = [{"name": t["name"], "description": t.get("description", ""),
                        "input_schema": t.get("parameters") or {"type": "object", "properties": {}}} for t in tools]
        self._voice, self._language = voice, language
        await self.stt.start(language)
        self._pump = asyncio.create_task(self._stt_pump())

    async def send_audio(self, pcm: bytes) -> None:
        self.usage["stt_seconds"] += len(pcm) / 2 / self.s.sample_rate
        await self.stt.send(pcm)

    async def send_tool_result(self, call_id: str, output: dict[str, Any]) -> None:
        fut = self._tool_waiters.get(call_id)
        if fut and not fut.done():
            fut.set_result(output)

    async def cancel_response(self) -> None:
        """Barge-in: corta de verdad el LLM (cierra el stream HTTP) y el TTS, no solo descarta su audio."""
        turn, self._turn = self._turn, None
        if turn and not turn.done():
            turn.cancel()
            await asyncio.gather(turn, return_exceptions=True)

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        while True:
            ev = await self._q.get()
            if ev is None:
                return
            yield ev

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        tasks = [t for t in (self._turn, self._pump) if t and not t.done()]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for fut in self._tool_waiters.values():
            fut.cancel()
        for c in (self.stt, self.llm, self.tts):
            try:
                await c.close()
            except Exception:  # noqa: BLE001
                log.warning("cascade_close_failed", client=type(c).__name__)
        await self._q.put(None)

    # ---- STT ----
    async def _stt_pump(self) -> None:
        try:
            async for text in self.stt.utterances():
                if not text.strip():
                    continue
                t_final = time.monotonic()
                await self.cancel_response()  # el usuario habló de nuevo: el turno anterior queda obsoleto
                await self._q.put({"type": "transcript_user", "text": text})
                await self._q.put({"type": "stage", "stage": "stt_final", "t": t_final})
                self._turn = asyncio.create_task(self._run_turn(text, t_final))
            if not self._closed:  # el STT cerró el flujo (tras agotar sus reintentos): la sesión decide qué hacer
                await self._q.put(None)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("stt_failed", error=type(exc).__name__)
            await self._q.put({"type": "error", "message": "stt_unavailable"})
            await self._q.put(None)

    # ---- turno: LLM -> frases -> TTS -> audio ----
    async def _run_turn(self, user_text: str, t_final: float) -> None:
        pending: list[dict[str, Any]] = [{"role": "user", "content": user_text}]
        spoken: list[str] = []
        completed = False
        try:
            async with asyncio.timeout(self.s.cascade_turn_timeout_s):
                for _ in range(self.s.cascade_max_tool_rounds):
                    tool_uses = await self._llm_round(pending, spoken, t_final)
                    if not tool_uses:
                        break
                    results = []
                    for tu in tool_uses:
                        out = await self._call_tool(tu)
                        results.append({"type": "tool_result", "tool_use_id": tu["id"], "content": json.dumps(out)})
                    pending.append({"role": "user", "content": results})  # un solo mensaje con todos los resultados
                # Coherente solo si no queda un tool_use sin su tool_result (p. ej. se agotaron las rondas).
                completed = pending[-1]["role"] == "user" or not isinstance(pending[-1]["content"], list) \
                    or not any(b.get("type") == "tool_use" for b in pending[-1]["content"])
            await self._q.put({"type": "response_done"})
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - un fallo de LLM/TTS no tumba la sesión: se avisa y se sigue escuchando
            log.warning("cascade_turn_failed", error=type(exc).__name__, detail=str(exc)[:200])
            await self._q.put({"type": "error", "message": "turn_failed"})
            await self._q.put({"type": "response_done"})
        finally:
            self._commit(pending, spoken, completed)

    async def _llm_round(self, pending: list[dict[str, Any]], spoken: list[str], t_final: float) -> list[dict[str, Any]]:
        """Una pasada del LLM: sus frases se sintetizan en paralelo a la generación. Devuelve las herramientas pedidas."""
        sentences: asyncio.Queue[str | None] = asyncio.Queue()
        tool_uses: list[dict[str, Any]] = []
        text_parts: list[str] = []
        first_sentence_at: list[float] = []

        async def produce() -> None:
            buf = ""
            try:
                async for ev in self.llm.stream(self._system, self._messages(pending), self._tools,
                                                self.s.cascade_max_tokens):
                    if ev["type"] == "text":
                        text_parts.append(ev["text"])
                        buf += ev["text"]
                        done, buf = split_sentences(buf)
                        for sent in done:
                            if not first_sentence_at:
                                first_sentence_at.append(time.monotonic())
                                await self._q.put({"type": "stage", "stage": "llm",
                                                   "ms": round((first_sentence_at[0] - t_final) * 1000, 1)})
                            await sentences.put(sent)
                    elif ev["type"] == "tool_use":
                        tool_uses.append(ev)
                    elif ev["type"] == "usage":
                        self.usage["llm_in_tokens"] += int(ev.get("in", 0))
                        self.usage["llm_out_tokens"] += int(ev.get("out", 0))
                rest, _ = split_sentences(buf, final=True)
                for sent in rest:
                    if not first_sentence_at:
                        first_sentence_at.append(time.monotonic())
                        await self._q.put({"type": "stage", "stage": "llm",
                                           "ms": round((first_sentence_at[0] - t_final) * 1000, 1)})
                    await sentences.put(sent)
            finally:
                await sentences.put(None)

        async def speak() -> None:
            first_audio_sent = False
            carry = b""
            while (sent := await sentences.get()) is not None:
                self.usage["tts_chars"] += len(sent)
                async for chunk in self.tts.synth(sent, self._voice, self._language):
                    if not first_audio_sent:
                        first_audio_sent = True
                        await self._q.put({"type": "stage", "stage": "tts",
                                           "ms": round((time.monotonic() - first_sentence_at[0]) * 1000, 1)})
                    data = carry + chunk
                    cut = len(data) - len(data) % FRAME_BYTES
                    carry = data[cut:]
                    for i in range(0, cut, FRAME_BYTES):
                        await self._q.put({"type": "audio_delta", "audio": data[i:i + FRAME_BYTES]})
                if carry:  # resto de la frase: se completa a un frame con silencio para no perder la cola
                    await self._q.put({"type": "audio_delta", "audio": carry.ljust(FRAME_BYTES, b"\x00")})
                    carry = b""
                spoken.append(sent)

        async with asyncio.TaskGroup() as tg:  # si una falla o se cancela, la otra se cancela con ella
            tg.create_task(produce())
            tg.create_task(speak())

        text = "".join(text_parts).strip()
        blocks: list[dict[str, Any]] = ([{"type": "text", "text": text}] if text else []) + [
            {"type": "tool_use", "id": t["id"], "name": t["name"], "input": t["input"]} for t in tool_uses]
        if blocks:
            pending.append({"role": "assistant", "content": blocks})
        return tool_uses

    async def _call_tool(self, tu: dict[str, Any]) -> dict[str, Any]:
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._tool_waiters[tu["id"]] = fut
        try:
            await self._q.put({"type": "tool_call", "call_id": tu["id"], "name": tu["name"],
                               "arguments": json.dumps(tu["input"])})
            return await fut  # lo resuelve send_tool_result; si el usuario interrumpe, cancel_response cancela el turno
        finally:
            self._tool_waiters.pop(tu["id"], None)

    # ---- historial ----
    def _messages(self, pending: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return self._history + pending

    def _commit(self, pending: list[dict[str, Any]], spoken: list[str], completed: bool) -> None:
        """Solo se guarda lo que consistió en un intercambio coherente: un turno interrumpido conserva lo que el
        usuario llegó a oír (nunca bloques tool_use sin su tool_result, que la API rechazaría)."""
        if completed:
            self._history.extend(pending)
        else:
            self._history.append(pending[0])
            if spoken:
                self._history.append({"role": "assistant", "content": " ".join(spoken)})
        limit = self.s.cascade_history_messages
        if len(self._history) > limit:
            self._history = self._history[-limit:]
        while self._history and not (self._history[0]["role"] == "user" and isinstance(self._history[0]["content"], str)):
            self._history.pop(0)  # nunca empezar el contexto en mitad de un par tool_use/tool_result


# ---------------------------------------------------------------- clientes concretos


class DeepgramSTT:
    """STT en streaming (WebSocket). Une los fragmentos finales hasta `speech_final`/`UtteranceEnd`.
    Si la conexión cae, reintenta con espera exponencial; los frames enviados mientras tanto se descartan."""

    def __init__(self, settings: Settings, max_retries: int = 3) -> None:
        self.s = settings
        self.max_retries = max_retries
        self.ws: websockets.WebSocketClientProtocol | None = None
        self._language: str | None = None
        self._connected = False

    def _url(self) -> str:
        q = (f"model={self.s.deepgram_stt_model}&encoding=linear16&sample_rate={self.s.sample_rate}&channels=1"
             f"&interim_results=true&smart_format=true&endpointing={self.s.deepgram_stt_endpointing_ms}"
             f"&utterance_end_ms=1000")
        if self._language:
            q += f"&language={self._language.split('-')[0]}"
        return f"{self.s.deepgram_stt_url}?{q}"

    async def _open(self) -> None:
        self.ws = await asyncio.wait_for(websockets.connect(
            self._url(), extra_headers={"Authorization": f"Token {self.s.deepgram_api_key}"}, max_size=2**22),
            timeout=self.s.provider_connect_timeout_s)
        self._connected = True

    async def start(self, language: str | None) -> None:
        self._language = language
        await self._open()

    async def send(self, pcm: bytes) -> None:
        if not self._connected or self.ws is None:
            return  # reconectando
        try:
            await self.ws.send(pcm)
        except websockets.ConnectionClosed:
            self._connected = False

    async def utterances(self) -> AsyncIterator[str]:
        parts: list[str] = []
        attempt = 0
        while True:
            try:
                assert self.ws is not None
                async for raw in self.ws:
                    attempt = 0
                    m = json.loads(raw)
                    if m.get("type") == "Results":
                        alt = (m.get("channel", {}).get("alternatives") or [{}])[0]
                        if m.get("is_final") and alt.get("transcript"):
                            parts.append(alt["transcript"])
                        if m.get("speech_final") and parts:
                            yield " ".join(parts)
                            parts = []
                    elif m.get("type") == "UtteranceEnd" and parts:
                        yield " ".join(parts)
                        parts = []
            except (websockets.ConnectionClosed, OSError, asyncio.TimeoutError):
                pass
            self._connected = False
            if attempt >= self.max_retries:
                return
            await asyncio.sleep(self.s.provider_reconnect_backoff_s * 2 ** attempt)
            attempt += 1
            try:
                await self._open()
                log.info("stt_reconnected", attempt=attempt)
            except (websockets.WebSocketException, OSError, asyncio.TimeoutError):
                continue

    async def close(self) -> None:
        self._connected = False
        if self.ws:
            try:
                await self.ws.send(json.dumps({"type": "CloseStream"}))
            except Exception:  # noqa: BLE001
                pass
            await self.ws.close()


class DeepgramTTS:
    """TTS en streaming por HTTP: devuelve PCM16 a la frecuencia de la plataforma sin contenedor."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.s = settings
        self.http = client or httpx.AsyncClient(timeout=httpx.Timeout(10.0, read=15.0))

    def _model(self, voice: str | None, language: str | None) -> str:
        if voice and voice in self.s.deepgram_tts_voices.values():
            return voice
        lang = (language or "es").split("-")[0]
        return self.s.deepgram_tts_voices.get(lang) or next(iter(self.s.deepgram_tts_voices.values()))

    async def synth(self, text: str, voice: str | None, language: str | None) -> AsyncIterator[bytes]:
        params = {"model": self._model(voice, language), "encoding": "linear16",
                  "sample_rate": str(self.s.sample_rate), "container": "none"}
        async with self.http.stream("POST", self.s.deepgram_tts_url, params=params, json={"text": text},
                                    headers={"Authorization": f"Token {self.s.deepgram_api_key}"}) as r:
            r.raise_for_status()
            async for chunk in r.aiter_bytes():
                if chunk:
                    yield chunk

    async def close(self) -> None:
        await self.http.aclose()


class AnthropicLLM:
    """Mensajes en streaming (SSE) con herramientas. Cancelar la tarea cierra la conexión y detiene la generación."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.s = settings
        self.http = client or httpx.AsyncClient(timeout=httpx.Timeout(10.0, read=20.0))

    async def stream(self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]],
                     max_tokens: int) -> AsyncIterator[dict[str, Any]]:
        body: dict[str, Any] = {
            "model": self.s.cascade_llm_model, "max_tokens": max_tokens, "stream": True,
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": messages}
        if tools:
            body["tools"] = tools
        headers = {"x-api-key": self.s.anthropic_api_key, "anthropic-version": "2023-06-01"}
        block: dict[str, Any] = {}
        usage_in = 0
        async with self.http.stream("POST", self.s.anthropic_url, json=body, headers=headers) as r:
            r.raise_for_status()
            async for line in r.aiter_lines():
                if not line.startswith("data:"):
                    continue
                m = json.loads(line[5:])
                t = m.get("type")
                if t == "message_start":
                    u = m["message"].get("usage", {})
                    usage_in = u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0) \
                        + u.get("cache_creation_input_tokens", 0)
                elif t == "content_block_start":
                    cb = m["content_block"]
                    block = {"type": cb["type"], "id": cb.get("id"), "name": cb.get("name"), "json": ""}
                elif t == "content_block_delta":
                    d = m["delta"]
                    if d["type"] == "text_delta":
                        yield {"type": "text", "text": d["text"]}
                    elif d["type"] == "input_json_delta":
                        block["json"] += d.get("partial_json", "")
                elif t == "content_block_stop":
                    if block.get("type") == "tool_use":
                        yield {"type": "tool_use", "id": block["id"], "name": block["name"],
                               "input": json.loads(block["json"] or "{}")}
                    block = {}
                elif t == "message_delta":
                    yield {"type": "usage", "in": usage_in, "out": m.get("usage", {}).get("output_tokens", 0)}
                elif t == "error":
                    raise RuntimeError(f"anthropic_error: {str(m.get('error'))[:200]}")

    async def close(self) -> None:
        await self.http.aclose()


def build_cascade(settings: Settings) -> CascadedProvider:
    return CascadedProvider(settings, DeepgramSTT(settings), AnthropicLLM(settings), DeepgramTTS(settings))
