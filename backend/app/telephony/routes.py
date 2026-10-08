"""Rutas de telefonía: webhook entrante, WebSocket de Media Streams y llamadas salientes."""
import asyncio
import base64
import hashlib
import json
import uuid
from typing import Any, Callable
from urllib.parse import parse_qs

import structlog
from fastapi import Depends, FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, Field

from app.core.config import Settings
from app.observability.metrics import ERRORS
from app.orchestration.session import SessionOptions, VoiceSession
from app.security.auth import Principal
from app.telephony.codec import platform_to_telephony, telephony_to_platform
from app.telephony.twilio import TwilioClient, dial_twiml, hangup_twiml, stream_twiml, validate_signature

log = structlog.get_logger()

WS_CLOSE_UNAUTHORIZED = 4401
WS_CLOSE_CAPACITY = 4429
MAX_MEDIA_BYTES = 4096  # un frame de Twilio son 160 bytes (20 ms); mucho más es anómalo


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
        org = s.telephony_org_id
        if not await app.state.store.hit("telephony", f"{org}:{caller_id_hash(who)}", s.rate_limit_sessions_per_min, 60):
            return Response(hangup_twiml("Demasiados intentos. Inténtelo más tarde."), media_type="text/xml")
        if await app.state.store.active_sessions(org) >= s.max_sessions_per_org:
            return Response(hangup_twiml("Todas las líneas están ocupadas. Inténtelo más tarde."), media_type="text/xml")
        principal = Principal(f"phone:{caller_id_hash(who)}", org, "customer")
        ticket = uuid.uuid4().hex
        await app.state.store.put_ticket(f"tel:{ticket}", principal, 60, s.telephony_agent_id or None)
        await app.state.db.audit(org, principal.user_id, "telephony.call_received",
                                 {"call_sid": call_sid, "direction": form.get("Direction", "inbound")})
        ws_url = public_url("/ws/telephony").replace("https://", "wss://", 1)
        return Response(stream_twiml(ws_url, {"ticket": ticket, "call_sid": call_sid}), media_type="text/xml")

    @app.post("/api/v1/telephony/calls", status_code=202)
    async def outbound_call(body: OutboundCallBody, p: Principal = Depends(authed("telephony:call"))):
        if not s.twilio_from_number:
            raise HTTPException(503, "TWILIO_FROM_NUMBER no configurado")
        try:
            call = await client.create_call(body.to, s.twilio_from_number, public_url("/telephony/voice", "direction=outbound"))
        except Exception:  # noqa: BLE001
            log.exception("outbound_call_failed")
            ERRORS.labels("telephony_outbound").inc()
            raise HTTPException(502, "El operador de telefonía rechazó la llamada")
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

                    async def on_close(_s: VoiceSession, org_id=org_id):
                        live.pop(session_key, None)
                        await app.state.store.release_session(org_id, session_key)

                    session = VoiceSession(
                        principal, make_provider(), registry, sender, s, persistence=app.state.db,
                        instructions=(agent["instructions"] if agent else
                                      "Eres un agente telefónico de atención al cliente. Responde breve y usa herramientas para datos reales."),
                        allowed_tools=set(agent["tools"]) if agent and agent["tools"] else None,
                        on_close=on_close,
                        # Sin consentimiento explícito no se graba: por teléfono se exigiría un aviso/confirmación previa.
                        options=SessionOptions(recording_consent=False,
                                               voice=(agent or {}).get("voice"),
                                               language=(agent or {}).get("language") or s.telephony_language),
                        transfer_handler=lambda reason, cs=call_sid: _transfer(cs),
                    )
                    live[session_key] = session
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
                await session.close()
            elif principal is not None:
                await app.state.store.release_session(principal.org_id, session_key)

    async def _transfer(call_sid: str) -> dict[str, Any]:
        if not s.human_transfer_number or not call_sid:
            return {"status": "unavailable"}

        async def later() -> None:
            await asyncio.sleep(s.transfer_announce_ms / 1000)  # deja que el agente termine de avisar
            try:
                await client.redirect_call(call_sid, dial_twiml(s.human_transfer_number, s.twilio_from_number or None))
            except Exception:  # noqa: BLE001
                log.exception("transfer_redirect_failed", call_sid=call_sid)
                ERRORS.labels("telephony_transfer").inc()

        t = asyncio.create_task(later())
        background.add(t)
        t.add_done_callback(background.discard)
        return {"status": "initiated"}
