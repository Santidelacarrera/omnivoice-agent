"""Orquestador por sesión: audio entrante, eventos del proveedor, herramientas y barge-in
coordinados con tareas asyncio y una máquina de estados."""
import asyncio
import json
import uuid
from typing import Any, Awaitable, Callable

import structlog

from app.core.config import Settings
from app.observability.metrics import ACTIVE_SESSIONS, ERRORS, SessionMetrics
from app.orchestration.state_machine import InvalidTransition, SessionStateMachine, State
from app.realtime.provider import RealtimeProvider
from app.realtime.vad import VoiceActivityDetector
from app.security.auth import Principal
from app.tools.registry import ToolRegistry

log = structlog.get_logger()

Send = Callable[[dict[str, Any] | bytes], Awaitable[None]]
AuditFn = Callable[[str, dict[str, Any]], Awaitable[None]]


class VoiceSession:
    def __init__(
        self,
        principal: Principal,
        provider: RealtimeProvider,
        registry: ToolRegistry,
        send: Send,
        settings: Settings,
        audit: AuditFn | None = None,
        instructions: str = "Eres un agente de atención al cliente. Responde breve y usa herramientas para datos reales.",
        allowed_tools: set[str] | None = None,
    ) -> None:
        self.id = uuid.uuid4().hex
        self.principal = principal
        self.provider = provider
        self.registry = registry
        self.send = send
        self.settings = settings
        self.audit = audit or self._noop_audit
        self.instructions = instructions
        self.allowed_tools = allowed_tools
        self.sm = SessionStateMachine()
        self.metrics = SessionMetrics(self.id, principal.org_id)
        self.vad = VoiceActivityDetector(settings.vad_energy_threshold, settings.vad_min_speech_ms, settings.sample_rate)
        self._tasks: set[asyncio.Task] = set()
        self._epoch = 0  # se incrementa en cada interrupción: descarta audio obsoleto
        self._agent_speaking = False
        self._closed = False

    @staticmethod
    async def _noop_audit(event: str, payload: dict[str, Any]) -> None:
        return None

    async def start(self) -> None:
        await self.provider.connect(self.instructions, self.registry.schemas(self.allowed_tools))
        self._spawn(self._provider_loop())
        ACTIVE_SESSIONS.inc()
        self._set(State.LISTENING)
        await self.send({"type": "session.ready", "session_id": self.id})
        await self.audit("session.started", {"session_id": self.id})

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
            log.warning("invalid_transition", session=self.id, frm=self.sm.state, to=state)

    # ---- entrada del cliente ----
    async def on_audio(self, pcm: bytes, seq: int | None = None) -> None:
        if self._closed:
            return
        if seq is not None:
            self.metrics.track_seq(seq)
        event = self.vad.feed(pcm)
        if event == "speech_start":
            await self.handle_barge_in()
        elif event == "speech_end":
            self.metrics.mark_user_speech_end()
            self._set(State.PROCESSING)
        await self.provider.send_audio(pcm)

    async def on_client_event(self, msg: dict[str, Any]) -> None:
        # El cliente avisa cuando su VAD local detecta voz (más rápido que el servidor).
        if msg.get("type") == "barge_in":
            await self.handle_barge_in()
        elif msg.get("type") == "end":
            await self.close()

    async def handle_barge_in(self) -> None:
        if not (self._agent_speaking or self.sm.state in (State.RESPONDING, State.TOOL_RUNNING)):
            self._set(State.LISTENING)
            return
        self.metrics.mark_barge_detected()
        self._epoch += 1
        self._agent_speaking = False
        self._set(State.INTERRUPTED)
        await self.provider.cancel_response()
        await self.send({"type": "audio.clear"})  # el cliente vacía buffers y silencia
        dt = self.metrics.mark_silenced()
        await self.audit("barge_in", {"session_id": self.id, "server_silence_s": dt})
        self._set(State.LISTENING)

    # ---- eventos del proveedor ----
    async def _provider_loop(self) -> None:
        try:
            async for ev in self.provider.events():
                await self._on_provider_event(ev)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            ERRORS.labels("provider_loop").inc()
            log.exception("provider_loop_failed", session=self.id)
            self._set(State.ERROR)
            await self.send({"type": "error", "message": "provider_unavailable"})

    async def _on_provider_event(self, ev: dict[str, Any]) -> None:
        t = ev["type"]
        if t == "audio_delta":
            if self.sm.state in (State.INTERRUPTED,):
                return  # audio obsoleto tras interrupción
            if self.sm.state != State.RESPONDING:
                self._set(State.RESPONDING)
            self._agent_speaking = True
            ttfb = self.metrics.mark_audio_out()
            if ttfb is not None:
                await self.send({"type": "metrics", "first_audio_ms": round(ttfb * 1000)})
            await self.send(ev["audio"])
        elif t in ("transcript_user", "transcript_agent"):
            await self.send({"type": t, "text": ev["text"]})
            await self.audit(t, {"session_id": self.id, "chars": len(ev["text"])})
        elif t == "tool_call":
            self._spawn(self._run_tool(ev, self._epoch))
        elif t == "response_done":
            self._agent_speaking = False
            if self.sm.state in (State.RESPONDING, State.PROCESSING):
                self._set(State.LISTENING)
            await self.send({"type": "state", "state": self.sm.state.value})
        elif t == "error":
            ERRORS.labels("provider_event").inc()
            await self.send({"type": "error", "message": ev["message"]})

    async def _run_tool(self, ev: dict[str, Any], epoch: int) -> None:
        self._set(State.TOOL_RUNNING)
        await self.send({"type": "tool.start", "name": ev["name"]})
        result = await self.registry.execute(
            ev["name"], ev["arguments"], self.principal, ev["call_id"], self.allowed_tools
        )
        await self.audit(
            "tool.executed",
            {"session_id": self.id, "tool": ev["name"], "ok": result.get("ok"), "error": result.get("error")},
        )
        await self.send({"type": "tool.end", "name": ev["name"], "ok": result.get("ok", False)})
        if epoch != self._epoch:
            return  # el usuario interrumpió; no se narra un resultado obsoleto
        self._set(State.PROCESSING)
        await self.provider.send_tool_result(ev["call_id"], result)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._set(State.COMPLETED)
        for t in list(self._tasks):
            t.cancel()
        await self.provider.close()
        ACTIVE_SESSIONS.dec()
        await self.audit(
            "session.completed",
            {"session_id": self.id, "interruptions": self.metrics.interruptions, "lost_packets": self.metrics.lost_packets},
        )
