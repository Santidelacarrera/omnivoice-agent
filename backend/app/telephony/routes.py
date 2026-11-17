"""Rutas de telefonía: webhook entrante, WebSocket de Media Streams y llamadas salientes."""
import asyncio
import base64
import hashlib
import json
import time
import uuid
from typing import Any, Callable
from urllib.parse import parse_qs
from xml.sax.saxutils import escape

import structlog
from fastapi import Depends, FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, Field

from app.core.config import Settings
from app.observability.metrics import CALL_EVENTS, ERRORS
from app.orchestration.session import SessionOptions, VoiceSession
from app.security.auth import Principal
from app.telephony import call_state as cs
from app.telephony.codec import platform_to_telephony, telephony_to_platform
from app.telephony.handoff import HANDOFF_TTL_S, build_briefing
from app.telephony.twilio import TwilioClient, dial_twiml, hangup_twiml, stream_twiml, validate_signature

log = structlog.get_logger()

WS_CLOSE_UNAUTHORIZED = 4401
WS_CLOSE_CAPACITY = 4429
MAX_MEDIA_BYTES = 4096  # un frame de Twilio son 160 bytes (20 ms); mucho más es anómalo


class TransferCallBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(default="", max_length=300)


class OutboundCallBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    to: str = Field(pattern=r"^\+[1-9]\d{6,14}$")  # E.164


def caller_id_hash(number: str) -> str:
    """El número de teléfono es dato personal: en logs/auditoría solo aparece un hash corto."""
    return hashlib.sha256(number.encode()).hexdigest()[:12]


class MediaStreamSender:
    """Convierte lo que emite la sesión (PCM de la plataforma, eventos) al protocolo de Media Streams."""

    def __init__(self, ws: WebSocket, platform_rate: int) -> None:
        self.ws = ws
        self.rate = platform_rate
        self.stream_sid: str | None = None

    async def __call__(self, msg: Any) -> None:
        if not self.stream_sid:
            return
        if isinstance(msg, (bytes, bytearray)):
            payload = base64.b64encode(platform_to_telephony(bytes(msg), self.rate)).decode()
            await self.ws.send_text(json.dumps({"event": "media", "streamSid": self.stream_sid, "media": {"payload": payload}}))
        elif isinstance(msg, dict) and msg.get("type") == "audio.clear":
            # Vacía el audio que Twilio aún tiene en cola: sin esto el agente seguiría hablando tras la interrupción.
            await self.ws.send_text(json.dumps({"event": "clear", "streamSid": self.stream_sid}))


def register_telephony(app: FastAPI, s: Settings, live: dict[str, VoiceSession], make_provider: Callable[[], Any],
                       registry: Any, authed: Callable[[str], Any], storage: Any = None,
                       twilio: TwilioClient | None = None) -> None:
    if s.telephony_provider != "twilio" and twilio is None:
        return
    client = twilio or TwilioClient(s.twilio_account_sid, s.twilio_auth_token)
    app.state.twilio = client
    background: set[asyncio.Task] = set()
    by_call: dict[tuple[str, str], VoiceSession] = {}  # (org, CallSid) → sesión viva, para dar contexto al traspaso

    async def track(org: str, sid: str, status: str, key: str, detail: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """Registra un evento de la llamada. Es idempotente y nunca rompe la llamada (ADR-6)."""
        if not sid:
            return None
        res = await app.state.db.apply_call_event(org, "twilio", sid, status, key, detail or {})
        if res:
            CALL_EVENTS.labels(status, "duplicate" if res["duplicate"] else "applied" if res["applied"] else "ignored").inc()
        return res

    def public_url(path: str, query: str = "") -> str:
        return s.telephony_public_url.rstrip("/") + path + (f"?{query}" if query else "")

    @app.post("/telephony/voice")
    async def inbound_voice(request: Request):
        # Twilio envía application/x-www-form-urlencoded; se parsea a mano para no añadir python-multipart.
        form = {k: v[0] for k, v in parse_qs((await request.body()).decode(), keep_blank_values=True).items()}
        if s.twilio_validate_signature:
            url = public_url(request.url.path, request.url.query)
            if not validate_signature(s.twilio_auth_token, url, form, request.headers.get("x-twilio-signature", "")):
                ERRORS.labels("telephony_bad_signature").inc()
                raise HTTPException(403, "Firma inválida")
        call_sid, who = form.get("CallSid", ""), form.get("From", "")
        outbound = form.get("Direction", "").startswith("outbound")
        # El número de la plataforma es el destino en llamadas entrantes y el origen en las salientes.
        ours = form.get("From", "") if outbound else form.get("To", "")
        mapped = await app.state.db.resolve_phone_number(ours) if ours else None
        if mapped:
            org, agent_id = mapped["org_id"], mapped["agent_id"] or ""
            if not agent_id or mapped["agent_status"] != "published":
                # Sin agente o sin publicar: no se contesta con otra cosa (un borrador o archivado no atiende clientes).
                ERRORS.labels("telephony_agent_unavailable").inc()
                if call_sid and await app.state.db.upsert_call(org, "twilio", call_sid, "outbound" if outbound else "inbound",
                                                                agent_id or None, ours, caller_id_hash(who)):
                    await track(org, call_sid, cs.FAILED, "rejected", {"reason": "agent_not_published" if agent_id else "no_agent"})
                await app.state.db.audit(org, "telephony", "telephony.call_rejected",
                                         {"call_sid": call_sid, "reason": "agent_not_published" if agent_id else "no_agent"})
                return Response(hangup_twiml("Este número no está disponible por ahora."), media_type="text/xml")
        elif s.telephony_allow_unmapped:
            org, agent_id = s.telephony_org_id, s.telephony_agent_id
        else:
            ERRORS.labels("telephony_unmapped").inc()
            log.warning("telephony_unmapped_number", number_hash=caller_id_hash(ours))
            return Response(hangup_twiml("El número marcado no está disponible."), media_type="text/xml")
        if not await app.state.store.hit("telephony", f"{org}:{caller_id_hash(who)}", s.rate_limit_sessions_per_min, 60):
            return Response(hangup_twiml("Demasiados intentos. Inténtelo más tarde."), media_type="text/xml")
        if await app.state.store.active_sessions(org) >= s.max_sessions_per_org:
            return Response(hangup_twiml("Todas las líneas están ocupadas. Inténtelo más tarde."), media_type="text/xml")
        if call_sid:
            call = await app.state.db.upsert_call(org, "twilio", call_sid, "outbound" if outbound else "inbound",
                                                  agent_id or None, ours or None, caller_id_hash(who))
            if call and not call["created"] and (call["status"] == cs.CONNECTED or cs.is_terminal(call["status"])):
                # Entrega duplicada de una llamada que ya se atendió o terminó: no se abre una segunda sesión.
                ERRORS.labels("telephony_duplicate_call").inc()
                await app.state.db.audit(org, "telephony", "telephony.call_duplicate", {"call_sid": call_sid, "status": call["status"]})
                return Response(hangup_twiml("Esta llamada ya fue atendida."), media_type="text/xml")
        principal = Principal(f"phone:{caller_id_hash(who)}", org, "customer")
        ticket = uuid.uuid4().hex
        await app.state.store.put_ticket(f"tel:{ticket}", principal, 60, agent_id or None)
        await app.state.db.audit(org, principal.user_id, "telephony.call_received",
                                 {"call_sid": call_sid, "direction": form.get("Direction", "inbound")})
        ws_url = public_url("/ws/telephony").replace("https://", "wss://", 1)
        return Response(stream_twiml(ws_url, {"ticket": ticket, "call_sid": call_sid}), media_type="text/xml")

    @app.post("/telephony/status", status_code=204)
    async def call_status(request: Request):
        """Avisos de estado del proveedor (ringing, answered, completed...). Idempotente: un reenvío no cambia nada."""
        form = {k: v[0] for k, v in parse_qs((await request.body()).decode(), keep_blank_values=True).items()}
        if s.twilio_validate_signature:
            url = public_url(request.url.path, request.url.query)
            if not validate_signature(s.twilio_auth_token, url, form, request.headers.get("x-twilio-signature", "")):
                ERRORS.labels("telephony_bad_signature").inc()
                raise HTTPException(403, "Firma inválida")
        sid, status = form.get("CallSid", ""), cs.from_twilio(form.get("CallStatus", ""))
        org = await app.state.db.resolve_call_org("twilio", sid) if sid else None
        if org and status:
            await track(org, sid, status, f"tw:{form.get('CallStatus')}:{form.get('SequenceNumber', '')}",
                        {"duration": form.get("CallDuration")} if form.get("CallDuration") else None)
        return Response(status_code=204)  # desconocida o sin estado conocido: 204 igualmente, el proveedor no debe reintentar

    @app.get("/api/v1/calls")
    async def list_calls(limit: int = 50, p: Principal = Depends(authed("calls:read"))):
        return {"items": await app.state.db.list_calls(p.org_id, max(1, min(limit, 200)))}

    @app.get("/api/v1/calls/{call_id}")
    async def get_call(call_id: str, p: Principal = Depends(authed("calls:read"))):
        try:
            uuid.UUID(call_id)
        except ValueError:
            raise HTTPException(404, "Llamada no encontrada") from None
        call = await app.state.db.get_call(p.org_id, call_id)
        if not call:
            raise HTTPException(404, "Llamada no encontrada")
        return call

    @app.post("/telephony/whisper")
    async def whisper(request: Request):
        """Lo pide Twilio cuando el operador contesta: el briefing se le dice solo a él, antes de unir las dos partes."""
        form = {k: v[0] for k, v in parse_qs((await request.body()).decode(), keep_blank_values=True).items()}
        if s.twilio_validate_signature:
            url = public_url(request.url.path, request.url.query)
            if not validate_signature(s.twilio_auth_token, url, form, request.headers.get("x-twilio-signature", "")):
                ERRORS.labels("telephony_bad_signature").inc()
                raise HTTPException(403, "Firma inválida")
        token = request.query_params.get("h", "")
        data = await app.state.store.take_session_options(f"handoff:{token}") if token else {}  # un solo uso
        text = data.get("text") if isinstance(data, dict) else None
        body = f'<Say language="es-ES">{escape(text)}</Say>' if text else ""
        return Response(f'<?xml version="1.0" encoding="UTF-8"?><Response>{body}</Response>', media_type="text/xml")

    async def _live_call(p: Principal, call_id: str) -> dict[str, Any]:
        try:
            uuid.UUID(call_id)
        except ValueError:
            raise HTTPException(404, "Llamada no encontrada") from None
        call = await app.state.db.get_call(p.org_id, call_id)
        if not call or call["provider"] != "twilio":
            raise HTTPException(404, "Llamada no encontrada")
        if cs.is_terminal(call["status"]):
            raise HTTPException(409, f"La llamada ya terminó ({call['status']})")
        return call

    @app.post("/api/v1/calls/{call_id}/transfer", status_code=202)
    async def transfer_call(call_id: str, body: TransferCallBody | None = None, p: Principal = Depends(authed("telephony:call"))):
        call = await _live_call(p, call_id)
        if call["status"] not in (cs.ANSWERED, cs.CONNECTED):
            raise HTTPException(409, "Solo se puede transferir una llamada atendida")
        outcome = await _transfer(call["sid"], p.org_id, (body.reason if body else "") or "", _context(p.org_id, call["sid"]), "api")
        if outcome.get("status") != "initiated":
            raise HTTPException(409, "Transferencia no disponible (sin número de operador configurado)")
        await app.state.db.audit(p.org_id, p.user_id, "call.transfer_requested", {"call_id": call_id, "source": "api"})
        return {"status": "initiated"}

    @app.post("/api/v1/calls/{call_id}/end", status_code=202)
    async def end_call(call_id: str, p: Principal = Depends(authed("telephony:call"))):
        call = await _live_call(p, call_id)
        before_answer = call["status"] in (cs.CREATED, cs.QUEUED, cs.RINGING)
        try:
            await client.hangup(call["sid"], cancel=before_answer)
        except Exception:  # noqa: BLE001
            log.exception("hangup_failed", call_sid=call["sid"])
            ERRORS.labels("telephony_hangup").inc()
            raise HTTPException(502, "El operador de telefonía rechazó la orden") from None
        await track(p.org_id, call["sid"], cs.CANCELLED if before_answer else cs.COMPLETED, "api:end", {"source": "api"})
        await app.state.db.audit(p.org_id, p.user_id, "call.end_requested", {"call_id": call_id, "source": "api"})
        return {"status": "ending"}

    @app.post("/api/v1/telephony/calls", status_code=202)
    async def outbound_call(body: OutboundCallBody, p: Principal = Depends(authed("telephony:call"))):
        if not s.twilio_from_number:
            raise HTTPException(503, "TWILIO_FROM_NUMBER no configurado")
        try:
            call = await client.create_call(body.to, s.twilio_from_number, public_url("/telephony/voice", "direction=outbound"),
                                            public_url("/telephony/status"))
        except Exception:  # noqa: BLE001
            log.exception("outbound_call_failed")
            ERRORS.labels("telephony_outbound").inc()
            raise HTTPException(502, "El operador de telefonía rechazó la llamada")
        if call.get("sid") and await app.state.db.upsert_call(p.org_id, "twilio", call["sid"], "outbound", None,
                                                              s.twilio_from_number, caller_id_hash(body.to)):
            await track(p.org_id, call["sid"], cs.QUEUED, "placed")
        await app.state.db.audit(p.org_id, p.user_id, "telephony.call_placed",
                                 {"call_sid": call.get("sid"), "to": caller_id_hash(body.to)})
        return {"call_sid": call.get("sid"), "status": call.get("status")}

    @app.websocket("/ws/telephony")
    async def ws_telephony(ws: WebSocket):
        await ws.accept()
        sender = MediaStreamSender(ws, s.sample_rate)
        session: VoiceSession | None = None
        session_key = uuid.uuid4().hex
        principal: Principal | None = None
        try:
            while True:
                raw = await asyncio.wait_for(ws.receive_text(), timeout=60)
                if len(raw) > MAX_MEDIA_BYTES * 2:
                    continue
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                event = msg.get("event")
                if event == "start" and session is None:
                    start = msg.get("start") or {}
                    params = start.get("customParameters") or {}
                    entry = await app.state.store.take_ticket(f"tel:{params.get('ticket', '')}")
                    if not entry:
                        await ws.close(code=WS_CLOSE_UNAUTHORIZED)
                        return
                    principal, agent_id = entry
                    if not await app.state.store.acquire_session(principal.org_id, session_key, s.max_sessions_per_org):
                        await ws.close(code=WS_CLOSE_CAPACITY)
                        return
                    sender.stream_sid = start.get("streamSid")
                    call_sid = start.get("callSid") or params.get("call_sid", "")
                    agent = await app.state.db.get_agent(principal.org_id, agent_id) if agent_id else None
                    org_id = principal.org_id

                    async def on_close(sess: VoiceSession, org_id=org_id, call_sid=call_sid):
                        live.pop(session_key, None)
                        by_call.pop((org_id, call_sid), None)
                        await track(org_id, call_sid, cs.COMPLETED, "ws:close")
                        await app.state.store.release_session(org_id, session_key)
                        try:  # mismo consumo diario que las sesiones web
                            await app.state.store.add_usage(org_id, time.monotonic() - sess.metrics.started_at)
                        except Exception:  # noqa: BLE001
                            log.exception("usage_accounting_failed", session=sess.id)

                    session = VoiceSession(
                        principal, make_provider(), registry, sender, s, persistence=app.state.db,
                        instructions=(agent["instructions"] if agent else
                                      "Eres un agente telefónico de atención al cliente. Responde breve y usa herramientas para datos reales."),
                        allowed_tools=set(agent["tools"]) if agent and agent["tools"] else None,
                        on_close=on_close, provider_factory=make_provider,
                        # Sin consentimiento explícito no se graba: por teléfono se exigiría un aviso/confirmación previa.
                        options=SessionOptions(recording_consent=False,
                                               voice=(agent or {}).get("voice"),
                                               language=(agent or {}).get("language") or s.telephony_language),
                        transfer_handler=lambda reason, sid=call_sid, o=org_id: _transfer(sid, o, reason, _context(o, sid)),
                    )
                    live[session_key] = session
                    if call_sid:
                        by_call[(org_id, call_sid)] = session
                    await track(org_id, call_sid, cs.CONNECTED, "ws:start")
                    try:
                        await session.start()
                    except Exception:  # noqa: BLE001
                        log.exception("telephony_session_start_failed")
                        ERRORS.labels("provider_connect").inc()
                        await client.redirect_call(call_sid, hangup_twiml("Servicio no disponible. Inténtelo más tarde.")) if call_sid else None
                        return
                elif event == "media" and session is not None:
                    payload = (msg.get("media") or {}).get("payload", "")
                    if not payload or len(payload) > MAX_MEDIA_BYTES:
                        continue
                    try:
                        ulaw = base64.b64decode(payload, validate=True)
                    except ValueError:
                        continue
                    await session.on_audio(telephony_to_platform(ulaw, s.sample_rate))
                elif event == "stop":
                    break
        except (WebSocketDisconnect, asyncio.TimeoutError):
            pass
        finally:
            if session is not None:
                await asyncio.shield(session.close())
            elif principal is not None:
                await app.state.store.release_session(principal.org_id, session_key)

    def _context(org_id: str, call_sid: str) -> list[tuple[str, str]]:
        sess = by_call.get((org_id, call_sid))
        return sess.recent_context() if sess else []

    async def _transfer(call_sid: str, org_id: str, reason: str = "", turns: list[tuple[str, str]] | None = None,
                        source: str = "agent") -> dict[str, Any]:
        if not s.human_transfer_number or not call_sid:
            return {"status": "unavailable"}
        whisper_url = None
        briefing = build_briefing(reason, turns or [], source)
        token = uuid.uuid4().hex  # un solo uso y 5 min: el contexto no se guarda en la base de datos
        try:
            await app.state.store.put_session_options(f"handoff:{token}", {"text": briefing}, HANDOFF_TTL_S)
            whisper_url = public_url("/telephony/whisper", f"h={token}")
        except Exception:  # noqa: BLE001 - sin contexto el traspaso sigue adelante
            log.exception("handoff_context_failed")
            ERRORS.labels("telephony_handoff_context").inc()
        await track(org_id, call_sid, cs.TRANSFERRED, "transfer", {"source": source, "context": whisper_url is not None})

        async def later() -> None:
            await asyncio.sleep(s.transfer_announce_ms / 1000)  # deja que el agente termine de avisar
            try:
                await client.redirect_call(call_sid, dial_twiml(s.human_transfer_number, s.twilio_from_number or None, whisper_url))
            except Exception:  # noqa: BLE001
                log.exception("transfer_redirect_failed", call_sid=call_sid)
                ERRORS.labels("telephony_transfer").inc()

        t = asyncio.create_task(later())
        background.add(t)
        t.add_done_callback(background.discard)
        return {"status": "initiated"}
