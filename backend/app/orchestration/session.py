"""Orquestador por sesión: audio entrante, eventos del proveedor, herramientas y barge-in
coordinados con tareas asyncio y una máquina de estados."""
import asyncio
import json
import time
import uuid
from collections import deque
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import structlog

from app.core.catalog import language_directive
from app.core.config import Settings
from app.observability.metrics import (ACTIVE_SESSIONS, COST_WINDOW, ERRORS, PROVIDER_RECONNECTS, RECORDINGS,
                                       SessionMetrics)
from app.orchestration.state_machine import InvalidTransition, SessionStateMachine, State
from app.realtime.provider import RealtimeProvider
from app.realtime.vad import create_vad
from app.security.auth import Principal
from app.services.persistence import InMemoryPersistence, Persistence
from app.services.recordings import StereoRecorder, recording_key
from app.tools.registry import ToolRegistry

log = structlog.get_logger()

Send = Callable[[dict[str, Any] | bytes], Awaitable[None]]


@dataclass(frozen=True)
class SessionOptions:
    """Opciones elegidas por la persona al crear la sesión (ya validadas por el API)."""

    recording_consent: bool = False
    voice: str | None = None
    language: str | None = None


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
        options: SessionOptions | None = None,
        recording_storage: Any = None,
        recording_allowed: bool = False,
        transfer_handler: Callable[[str], Awaitable[dict[str, Any]]] | None = None,
        provider_factory: Callable[[], RealtimeProvider] | None = None,
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
        self.options = options or SessionOptions()
        self.recording_storage = recording_storage
        # Derivación efectiva a un humano (p. ej. telefonía). Sin handler, `transfer_to_human` solo deja constancia.
        self.transfer_handler = transfer_handler
        # Se graba solo con las tres condiciones: almacenamiento configurado, política de la organización y consentimiento.
        self.recorder: StereoRecorder | None = (
            StereoRecorder(settings.sample_rate, settings.max_session_seconds)
            if recording_storage is not None and recording_allowed and self.options.recording_consent else None)
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
        # Reconexión: con fábrica, un fallo del proveedor se intenta recuperar (acotado) antes de dar la sesión por perdida.
        self.provider_factory = provider_factory
        self._recovering = False
        self._reconnects = 0
        self._recent: deque[tuple[str, str]] = deque(maxlen=6)  # solo en memoria; contexto al reconectar
        self._usage_carry: dict[str, float] = {}
        self._audio_out_bytes = 0
        self._full_instructions = ""
        self.cost_usd: float | None = None
        self._stages: dict[str, float] = {}

    async def _audit(self, action: str, detail: dict[str, Any] | None = None) -> None:
        d = {"session_id": self.id, "correlation_id": self.correlation_id, **(detail or {})}
        log.info("audit", action=action, org=self.principal.org_id, **d)
        await self.db.audit(self.principal.org_id, self.principal.user_id, action, d)

    async def start(self) -> None:
        self.conversation_id = await self.db.start_conversation(self.principal.org_id, self.id, self.principal.user_id)
        self._full_instructions = self.instructions + language_directive(self.options.language)
        await asyncio.wait_for(
            self.provider.connect(self._full_instructions, self.registry.schemas(self.allowed_tools),
                                  voice=self.options.voice, language=self.options.language),
            timeout=self.settings.provider_connect_timeout_s)
        if self.conversation_id:
            await self.db.set_conversation_options(
                self.principal.org_id, self.conversation_id, recording_consent=self.recorder is not None,
                voice=self.options.voice, language=self.options.language)
        self._spawn(self._provider_loop(self.provider))
        ACTIVE_SESSIONS.inc()
        self._counted = True
        self._set(State.LISTENING)
        await self.send({"type": "session.ready", "session_id": self.id, "recording": self.recorder is not None,
                         "voice": self.options.voice, "language": self.options.language})
        await self._audit("session.started")
        if self.options.recording_consent:  # constancia del consentimiento (y de si se pudo atender)
            await self._audit("recording.consent", {"granted": True, "recording": self.recorder is not None})

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
        if self.recorder:
            self.recorder.add_user(pcm)
        if seq is not None:
            self.metrics.track_seq(seq)
        event = self.vad.feed(pcm)
        if self.vad.speaking and self.vad.last_voiced:
            self.metrics.mark_voice()  # el fin del turno es el ÚLTIMO chunk con voz, no el fin declarado por el VAD
        if event == "speech_start":
            self._cancel_hint()  # el servidor confirmó voz: la interrupción se resuelve aquí
            await self.handle_barge_in()
        elif event == "speech_end":
            self._set(State.PROCESSING)
        if self._provider_failed or self._recovering:
            return  # reconectando: el audio de ese intervalo se descarta (no se acumula sin límite)
        try:
            await self.provider.send_audio(pcm)
        except Exception as exc:  # noqa: BLE001 - el proveedor cerró o rechazó la conexión
            await self._provider_failure("send_audio", exc)

    async def on_client_event(self, msg: dict[str, Any]) -> None:
        # El cliente avisa cuando su VAD local detecta voz (más rápido que el servidor).
        if msg.get("type") == "barge_in":
            await self._on_barge_in_hint()
        elif msg.get("type") == "recording_revoke":
            await self._revoke_recording()
        elif msg.get("type") == "end":
            await self.close()

    async def _revoke_recording(self) -> None:
        """La persona retira el consentimiento durante la llamada: se descarta lo acumulado y no se sube nada."""
        if not self.recorder:
            return
        self.recorder.discard()
        self.recorder = None
        RECORDINGS.labels("revoked").inc()
        if self.conversation_id:
            await self.db.set_conversation_options(
                self.principal.org_id, self.conversation_id, recording_consent=False,
                voice=self.options.voice, language=self.options.language)
        await self._audit("recording.revoked")
        await self.send({"type": "recording", "active": False})

    async def _finalize_recording(self) -> None:
        rec, self.recorder = self.recorder, None
        if rec is None:
            return
        if rec.seconds <= 0:
            rec.discard()
            return
        try:
            seconds = rec.seconds
            wav = await asyncio.to_thread(rec.to_wav)
            key = recording_key(self.principal.org_id, self.conversation_id or self.id)
            await self.recording_storage.put(key, wav)
            if self.conversation_id:
                await self.db.add_recording(self.principal.org_id, self.conversation_id, key, len(wav), seconds)
            RECORDINGS.labels("stored").inc()
            await self._audit("recording.stored", {"bytes": len(wav), "seconds": round(seconds, 2),
                                                   "truncated": rec.truncated})
        except Exception:  # noqa: BLE001 - un fallo de almacenamiento no debe romper el cierre de la sesión
            RECORDINGS.labels("failed").inc()
            log.exception("recording_store_failed", session=self.id)
            await self._audit("recording.failed")

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
        if self.recorder:
            self.recorder.drop_pending_agent_audio()  # lo que no llegó a sonar no se graba
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
    async def _provider_loop(self, prov: RealtimeProvider) -> None:
        try:
            async for ev in prov.events():
                if prov is not self.provider:
                    return  # proveedor sustituido tras una reconexión: sus eventos ya no cuentan
                await self._on_provider_event(ev)
            if not self._closed and prov is self.provider:
                # El proveedor cerró el flujo sin error: sin esto el usuario se queda hablando al vacío.
                await self._provider_failure("stream_closed", None)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            if prov is self.provider:
                ERRORS.labels("provider_loop").inc()
                log.exception("provider_loop_failed", session=self.id)
                await self._provider_failure("provider_loop", exc)

    async def _on_provider_event(self, ev: dict[str, Any]) -> None:
        t = ev["type"]
        if t == "audio_delta":
            if self.sm.state == State.INTERRUPTED:
                return  # audio obsoleto tras interrupción
            if self.sm.state != State.RESPONDING:
                self._set(State.RESPONDING)
            self._agent_speaking = True
            ttfb = None if self.vad.last_voiced else self.metrics.mark_audio_out()  # no cuenta si el usuario sigue hablando
            if ttfb is not None:
                self.first_audio_ms = round(ttfb * 1000)
                await self.send({"type": "metrics", "first_audio_ms": self.first_audio_ms, **self._stages})
                self._stages = {}
            self._audio_out_bytes += len(ev["audio"])
            await self.send(ev["audio"])
            if self.recorder:
                self.recorder.add_agent(ev["audio"])
        elif t in ("transcript_user", "transcript_agent"):
            await self.send({"type": t, "text": ev["text"]})
            self._recent.append(("user" if t == "transcript_user" else "agent", ev["text"]))
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
        elif t == "stage":
            if ev["stage"] == "stt_final":
                if self.metrics.last_voice_at is not None:
                    stt = ev["t"] - self.metrics.last_voice_at
                    self.metrics.mark_stage("stt", stt)
                    self._stages["stt"] = round(max(0.0, stt) * 1000, 1)
            else:
                self.metrics.mark_stage(ev["stage"], ev["ms"] / 1000)
                self._stages[ev["stage"]] = ev["ms"]
        elif t == "error":
            ERRORS.labels("provider_event").inc()
            log.warning("provider_error_event", session=self.id, message=str(ev.get("message"))[:300])
            await self.send({"type": "error", "message": ev["message"]})

    async def _provider_failure(self, where: str, exc: Exception | None) -> None:
        """Registra el motivo real (visible en los logs) y, si hay fábrica y presupuesto de reintentos, reconecta;
        si no, avisa una sola vez al cliente y deja la sesión en ERROR."""
        if self._provider_failed or self._recovering or self._closed:
            return
        log.warning("provider_failed", session=self.id, where=where,
                    error=type(exc).__name__ if exc else "closed", detail=str(exc)[:300] if exc else "")
        if self.provider_factory is not None and self._reconnects < self.settings.provider_max_reconnects:
            self._recovering = True
            self._spawn(self._reconnect(where))
            return
        await self._fail_terminal(where)

    async def _fail_terminal(self, where: str) -> None:
        if self._provider_failed:
            return
        self._provider_failed = True
        self._recovering = False
        ERRORS.labels("provider_loop").inc()
        self._set(State.ERROR)
        await self.send({"type": "error", "message": "provider_unavailable"})
        await self._audit("session.provider_error", {"where": where, "reconnects": self._reconnects})

    def _recovery_context(self) -> str:
        if not self._recent:
            return ""
        lines = "\n".join(f"{'Usuario' if who == 'user' else 'Agente'}: {txt}" for who, txt in self._recent)
        return ("\n\nLa conexión se interrumpió y se ha restablecido. Continúa la conversación sin saludar de nuevo. "
                f"Últimos intercambios:\n{lines}")

    async def _reconnect(self, where: str) -> None:
        """Sustituye el proveedor caído. El turno en curso se da por perdido (época nueva, audio vaciado)."""
        self._epoch += 1
        self._agent_speaking = False
        if self.sm.state in (State.RESPONDING, State.TOOL_RUNNING):
            self._set(State.INTERRUPTED)
        self._set(State.LISTENING)
        await self.send({"type": "provider.reconnecting"})
        await self.send({"type": "audio.clear"})
        old = self.provider
        for k, v in (getattr(old, "usage", None) or {}).items():
            self._usage_carry[k] = self._usage_carry.get(k, 0) + v
        try:
            await asyncio.wait_for(old.close(), timeout=3)
        except Exception:  # noqa: BLE001 - el proveedor viejo ya estaba roto
            pass
        for attempt in range(self.settings.provider_max_reconnects):
            if self._closed:
                return
            self._reconnects += 1
            await asyncio.sleep(self.settings.provider_reconnect_backoff_s * 2 ** attempt)
            new = self.provider_factory()  # type: ignore[misc]
            try:
                await asyncio.wait_for(
                    new.connect(self._full_instructions + self._recovery_context(),
                                self.registry.schemas(self.allowed_tools),
                                voice=self.options.voice, language=self.options.language),
                    timeout=self.settings.provider_connect_timeout_s)
            except asyncio.CancelledError:  # la sesión se cerró en plena conexión: no dejar el proveedor nuevo huérfano
                try:
                    await asyncio.shield(new.close())
                except Exception:  # noqa: BLE001
                    pass
                raise
            except Exception as exc:  # noqa: BLE001
                PROVIDER_RECONNECTS.labels("failed").inc()
                log.warning("provider_reconnect_failed", session=self.id, attempt=attempt + 1,
                            error=type(exc).__name__)
                try:
                    await new.close()
                except Exception:  # noqa: BLE001
                    pass
                continue
            if self._closed:  # la sesión terminó mientras se reconectaba: no dejar el proveedor nuevo huérfano
                await new.close()
                return
            self.provider = new
            self._recovering = False
            self._spawn(self._provider_loop(new))
            PROVIDER_RECONNECTS.labels("ok").inc()
            await self.send({"type": "provider.reconnected", "attempt": attempt + 1})
            await self._audit("session.provider_reconnected", {"where": where, "attempt": attempt + 1})
            return
        await self._fail_terminal(where)

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
        if ev["name"] == "transfer_to_human" and result.get("ok") and self.transfer_handler:
            reason = str((result.get("data") or {}).get("reason", ""))
            try:
                outcome = await self.transfer_handler(reason)
            except Exception:  # noqa: BLE001
                log.exception("transfer_failed", session=self.id)
                outcome = {"status": "failed"}
            await self._audit("transfer.requested", {"status": outcome.get("status")})
            ok = outcome.get("status") == "initiated"
            result = {"ok": ok, "data": {"transfer": outcome.get("status")}} if ok else {
                "ok": False, "error": "transferencia_no_disponible"}
        # El resultado va solo al cliente de la propia sesión (para mostrarlo); no se registra en logs.
        await self.send({"type": "tool.end", "name": ev["name"], "ok": result.get("ok", False),
                         "data": result.get("data"), "error": result.get("error")})
        if epoch != self._epoch:
            return  # el usuario interrumpió; no se narra un resultado obsoleto
        self._set(State.PROCESSING)
        await self.provider.send_tool_result(ev["call_id"], result)

    def _provider_usage(self) -> dict[str, float]:
        usage = dict(self._usage_carry)
        for k, v in (getattr(self.provider, "usage", None) or {}).items():
            usage[k] = usage.get(k, 0) + v
        return usage

    def _cost_components(self) -> dict[str, float]:
        """Coste estimado = consumo MEDIDO × precio configurado (`price_*` en Settings). No incluye red ni cómputo propio."""
        s = self.settings
        usage = self._provider_usage()
        if "llm_in_tokens" in usage:  # cascada: se factura cada servicio por separado
            return {"stt": usage["stt_seconds"] / 60 * s.price_stt_per_min,
                    "llm": usage["llm_in_tokens"] / 1e6 * s.price_llm_in_per_mtok
                           + usage["llm_out_tokens"] / 1e6 * s.price_llm_out_per_mtok,
                    "tts": usage["tts_chars"] / 1000 * s.price_tts_per_1k_chars}
        return {"s2s_in": self._audio_in_bytes / 2 / s.sample_rate / 60 * s.price_s2s_in_per_min,
                "s2s_out": self._audio_out_bytes / 2 / s.sample_rate / 60 * s.price_s2s_out_per_min}

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
        await self._finalize_recording()
        if self._counted:
            ACTIVE_SESSIONS.dec()
        seconds = self._audio_in_bytes / 2 / self.settings.sample_rate
        duration = time.monotonic() - self.metrics.started_at
        cost = self._cost_components()
        self.cost_usd = round(sum(cost.values()), 6)
        COST_WINDOW.add(duration, cost)
        summary = {"final_state": final_name, "first_audio_ms": self.first_audio_ms,
                   "interruptions": self.metrics.interruptions, "lost_packets": self.metrics.lost_packets,
                   "audio_seconds": round(seconds, 2), "duration_s": round(duration, 2), "cost_usd": self.cost_usd,
                   "reconnects": self._reconnects}
        if self.conversation_id:
            await self.db.end_conversation(self.principal.org_id, self.conversation_id, summary)
        await self._audit("session.completed", summary)
        if self.on_close:
            await self.on_close(self)
        try:  # el cliente sabe así que la grabación ya está guardada y que puede colgar
            await self.send({"type": "session.closed", "final_state": final_name})
        except Exception:  # noqa: BLE001 - el socket puede estar ya cerrado
            pass
