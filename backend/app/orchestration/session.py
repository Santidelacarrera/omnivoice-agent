"""Orquestador por sesión: audio entrante, eventos del proveedor, herramientas y barge-in
coordinados con tareas asyncio y una máquina de estados."""
import asyncio
import json
import time
import uuid
from typing import Any, Awaitable, Callable

import structlog

from app.core.config import Settings
from app.observability.metrics import ACTIVE_SESSIONS, ERRORS, SessionMetrics
from app.orchestration.state_machine import InvalidTransition, SessionStateMachine, State
from app.realtime.provider import RealtimeProvider
from app.realtime.vad import create_vad
from app.security.auth import Principal
from app.services.persistence import InMemoryPersistence, Persistence
from app.tools.registry import ToolRegistry

log = structlog.get_logger()

Send = Callable[[dict[str, Any] | bytes], Awaitable[None]]


class VoiceSession:
    def __init__(
        self,
        principal: Principal,
        provider: RealtimeProvider,
        registry: ToolRegistry,
        send: Send,
        settings: Settings,
        persistence: Persistence | None = None,
        instructions: str = "Eres un agente de atención al cliente. Responde breve y usa herramientas para datos reales.",
        allowed_tools: set[str] | None = None,
        on_close: Callable[["VoiceSession"], Awaitable[None]] | None = None,
    ) -> None:
        self.id = uuid.uuid4().hex
        self.correlation_id = uuid.uuid4().hex  # une logs, eventos y auditoría de esta sesión
        self.principal = principal
        self.provider = provider
        self.registry = registry
        self.send = send
        self.settings = settings
        self.db = persistence or InMemoryPersistence()
        self.instructions = instructions
        self.allowed_tools = allowed_tools
        self.on_close = on_close
        self.sm = SessionStateMachine()
        self.metrics = SessionMetrics(self.id, principal.org_id)
        self.vad = create_vad(settings)
        self._hint_task: asyncio.Task | None = None
        self.conversation_id: str | None = None
        self.first_audio_ms: int | None = None
        self._audio_in_bytes = 0
        self._tasks: set[asyncio.Task] = set()
        self._epoch = 0  # se incrementa en cada interrupción: descarta audio y resultados obsoletos
        self._agent_speaking = False
        self._closed = False
        self._provider_failed = False
        self._counted = False  # ACTIVE_SESSIONS solo se decrementa si se incrementó

    async def _audit(self, action: str, detail: dict[str, Any] | None = None) -> None:
        d = {"session_id": self.id, "correlation_id": self.correlation_id, **(detail or {})}
        log.info("audit", action=action, org=self.principal.org_id, **d)
        await self.db.audit(self.principal.org_id, self.principal.user_id, action, d)

    async def start(self) -> None:
        self.conversation_id = await self.db.start_conversation(self.principal.org_id, self.id, self.principal.user_id)
        await self.provider.connect(self.instructions, self.registry.schemas(self.allowed_tools))
        self._spawn(self._provider_loop())
        ACTIVE_SESSIONS.inc()
        self._counted = True
        self._set(State.LISTENING)
        await self.send({"type": "session.ready", "session_id": self.id})
        await self._audit("session.started")

    def _spawn(self, coro) -> asyncio.Task:
        t = asyncio.create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)
        return t

    def _set(self, state: State) -> None:
        try:
            self.sm.to(state)
        except InvalidTransition:
            ERRORS.labels("invalid_transition").inc()
            log.warning("invalid_transition", session=self.id, frm=self.sm.state.value, to=state.value)

    # ---- entrada del cliente ----
    async def on_audio(self, pcm: bytes, seq: int | None = None) -> None:
        if self._closed or self._provider_failed:
            return  # sin proveedor no hay conversación: no se procesa más audio ni se ensucia la máquina de estados
        self._audio_in_bytes += len(pcm)
        if seq is not None:
            self.metrics.track_seq(seq)
        event = self.vad.feed(pcm)
        if event == "speech_start":
            self._cancel_hint()  # el servidor confirmó voz: la interrupción se resuelve aquí
            await self.handle_barge_in()
        elif event == "speech_end":
            self.metrics.mark_user_speech_end()
            self._set(State.PROCESSING)
        if self._provider_failed:
            return
        try:
            await self.provider.send_audio(pcm)
        except Exception as exc:  # noqa: BLE001 - el proveedor cerró o rechazó la conexión
            await self._provider_failure("send_audio", exc)

    async def on_client_event(self, msg: dict[str, Any]) -> None:
        # El cliente avisa cuando su VAD local detecta voz (más rápido que el servidor).
        if msg.get("type") == "barge_in":
            await self._on_barge_in_hint()
        elif msg.get("type") == "end":
            await self.close()

    async def _on_barge_in_hint(self) -> None:
        """Pista del navegador: su VAD (por energía) oyó algo y ya pausó la reproducción.

        Con VAD de modelo en el servidor, la pista no basta: se interrumpe solo si el servidor confirma voz
        en `barge_in_confirm_ms`; si era ruido, se ordena reanudar el audio. Con VAD por energía no hay un
        segundo criterio mejor, así que se interrumpe de inmediato como antes.
        """
        from app.realtime.vad import ModelVoiceActivityDetector

        if not isinstance(self.vad, ModelVoiceActivityDetector) or self.vad.speaking:
            if not await self.handle_barge_in():
                await self.send({"type": "audio.resume"})
            return
        self._cancel_hint()
        self._hint_task = self._spawn(self._hint_timeout())

    def _cancel_hint(self) -> None:
        if self._hint_task and not self._hint_task.done() and self._hint_task is not asyncio.current_task():
            self._hint_task.cancel()
        self._hint_task = None

    async def _hint_timeout(self) -> None:
        await asyncio.sleep(self.settings.barge_in_confirm_ms / 1000)
        self._hint_task = None
        ERRORS.labels("barge_in_unconfirmed").inc()  # ruido o eco que el navegador tomó por voz
        await self.send({"type": "audio.resume"})

    async def handle_barge_in(self) -> bool:
        """Interrumpe al agente si está hablando. Devuelve True si hubo algo que interrumpir."""
        if not (self._agent_speaking or self.sm.state in (State.RESPONDING, State.TOOL_RUNNING)):
            self._set(State.LISTENING)
            return False
        self.metrics.mark_barge_detected()
        self._epoch += 1
        self._agent_speaking = False
        self._set(State.INTERRUPTED)
        await self.provider.cancel_response()
        await self.send({"type": "audio.clear"})  # el cliente vacía buffers y silencia
        dt = self.metrics.mark_silenced()
        await self._audit("barge_in", {"server_silence_s": dt})
        if self.conversation_id:
            await self.db.add_event(self.principal.org_id, self.conversation_id, "barge_in",
                                    {"server_silence_s": dt}, self.correlation_id)
        self._set(State.LISTENING)
        return True

    # ---- eventos del proveedor ----
    async def _provider_loop(self) -> None:
        try:
            async for ev in self.provider.events():
                await self._on_provider_event(ev)
            if not self._closed and not self._provider_failed:
                # El proveedor cerró el flujo sin error: sin esto el usuario se queda hablando al vacío.
                await self._provider_failure("stream_closed", None)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            ERRORS.labels("provider_loop").inc()
            log.exception("provider_loop_failed", session=self.id)
            self._provider_failed = True
            self._set(State.ERROR)
            await self.send({"type": "error", "message": "provider_unavailable"})
            await self._audit("session.provider_error")

    async def _on_provider_event(self, ev: dict[str, Any]) -> None:
        t = ev["type"]
        if t == "audio_delta":
            if self.sm.state == State.INTERRUPTED:
                return  # audio obsoleto tras interrupción
            if self.sm.state != State.RESPONDING:
                self._set(State.RESPONDING)
            self._agent_speaking = True
            ttfb = self.metrics.mark_audio_out()
            if ttfb is not None:
                self.first_audio_ms = round(ttfb * 1000)
                await self.send({"type": "metrics", "first_audio_ms": self.first_audio_ms})
            await self.send(ev["audio"])
        elif t in ("transcript_user", "transcript_agent"):
            await self.send({"type": t, "text": ev["text"]})
            if self.conversation_id:
                speaker = "user" if t == "transcript_user" else "agent"
                await self.db.add_transcript(self.principal.org_id, self.conversation_id, speaker, ev["text"])
        elif t == "tool_call":
            self._spawn(self._run_tool(ev, self._epoch))
        elif t == "response_done":
            self._agent_speaking = False
            if self.sm.state in (State.RESPONDING, State.PROCESSING):
                self._set(State.LISTENING)
            await self.send({"type": "state", "state": self.sm.state.value})
        elif t == "error":
            ERRORS.labels("provider_event").inc()
            log.warning("provider_error_event", session=self.id, message=str(ev.get("message"))[:300])
            await self.send({"type": "error", "message": ev["message"]})

    async def _provider_failure(self, where: str, exc: Exception | None) -> None:
        """Registra el motivo real (visible en `docker compose logs backend`) y avisa una sola vez al cliente."""
        if self._provider_failed:
            return
        self._provider_failed = True
        ERRORS.labels("provider_loop").inc()
        log.warning("provider_failed", session=self.id, where=where,
                    error=type(exc).__name__ if exc else "closed", detail=str(exc)[:300] if exc else "")
        self._set(State.ERROR)
        await self.send({"type": "error", "message": "provider_unavailable"})
        await self._audit("session.provider_error", {"where": where})

    async def _run_tool(self, ev: dict[str, Any], epoch: int) -> None:
        self._set(State.TOOL_RUNNING)
        await self.send({"type": "tool.start", "name": ev["name"]})
        started = time.monotonic()
        result = await self.registry.execute(
            ev["name"], ev["arguments"], self.principal, ev["call_id"], self.allowed_tools
        )
        duration_ms = round((time.monotonic() - started) * 1000)
        await self._audit("tool.executed", {"tool": ev["name"], "ok": result.get("ok"),
                                            "error": result.get("error"), "duration_ms": duration_ms})
        # No se guardan los argumentos crudos (pueden contener datos personales); solo claves.
        try:
            arg_keys = sorted(json.loads(ev["arguments"]).keys()) if isinstance(ev["arguments"], str) else sorted(ev["arguments"])
        except Exception:  # noqa: BLE001
            arg_keys = []
        await self.db.record_tool(self.principal.org_id, self.conversation_id, {
            "tool": ev["name"], "call_id": ev["call_id"], "args": {"keys": arg_keys},
            "result": {"ok": result.get("ok"), "error": result.get("error")},
            "ok": result.get("ok"), "duration_ms": duration_ms})
        await self.send({"type": "tool.end", "name": ev["name"], "ok": result.get("ok", False)})
        if epoch != self._epoch:
            return  # el usuario interrumpió; no se narra un resultado obsoleto
        self._set(State.PROCESSING)
        await self.provider.send_tool_result(ev["call_id"], result)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        final = State.ERROR if self.sm.state == State.ERROR else State.COMPLETED
        final_name = self.sm.state.value if final == State.ERROR else State.COMPLETED.value
        self._set(State.COMPLETED)
        for t in list(self._tasks):
            t.cancel()
        await self.provider.close()
        if self._counted:
            ACTIVE_SESSIONS.dec()
        seconds = self._audio_in_bytes / 2 / self.settings.sample_rate
        summary = {"final_state": final_name, "first_audio_ms": self.first_audio_ms,
                   "interruptions": self.metrics.interruptions, "lost_packets": self.metrics.lost_packets,
                   "audio_seconds": round(seconds, 2)}
        if self.conversation_id:
            await self.db.end_conversation(self.principal.org_id, self.conversation_id, summary)
        await self._audit("session.completed", summary)
        if self.on_close:
            await self.on_close(self)
