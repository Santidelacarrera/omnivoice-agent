import json
import struct
import time
import uuid

import structlog
from fastapi import Depends, FastAPI, HTTPException, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field

from app.core.config import get_settings
from app.observability.metrics import ACTIVE_SESSIONS
from app.orchestration.session import VoiceSession
from app.realtime.provider import FakeProvider, OpenAIRealtimeProvider
from app.security.auth import Principal, decode_token, issue_token, require
from app.tools.handlers import InMemoryRepository, build_registry

structlog.configure(processors=[structlog.processors.TimeStamper(fmt="iso"), structlog.processors.JSONRenderer()])
log = structlog.get_logger()
settings = get_settings()

app = FastAPI(title=settings.app_name, version="1.0.0")
app.add_middleware(
    CORSMiddleware, allow_origins=settings.cors_origins, allow_methods=["*"], allow_headers=["*"], allow_credentials=True
)

repo = InMemoryRepository()  # Sustituible por PostgresRepository (ver database/)
registry = build_registry(repo)

# Tickets de conexión WS de un solo uso y corta vida (evita exponer el JWT en la URL).
_tickets: dict[str, tuple[Principal, float]] = {}
AUDIT: list[dict] = []  # buffer en memoria; en producción se escribe en audit_logs


async def audit(event: str, payload: dict) -> None:
    rec = {"ts": time.time(), "event": event, **payload}
    AUDIT.append(rec)
    log.info("audit", **rec)


class DevLogin(BaseModel):
    user_id: str = Field(min_length=1)
    org_id: str = Field(min_length=1)
    role: str = "customer"


@app.post("/api/v1/auth/dev-token")
async def dev_token(body: DevLogin):
    if settings.environment == "production":
        raise HTTPException(404)
    return {"token": issue_token(body.user_id, body.org_id, body.role)}


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/metrics")
async def prom_metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/api/v1/sessions")
async def create_session(p: Principal = Depends(require("session:create"))):
    if ACTIVE_SESSIONS._value.get() >= settings.max_sessions_per_org * 20:  # tope global defensivo
        raise HTTPException(429, "Capacidad agotada")
    ticket = uuid.uuid4().hex
    _tickets[ticket] = (p, time.time() + 30)
    await audit("session.ticket_issued", {"org": p.org_id, "user": p.user_id})
    return {"ticket": ticket, "ws_path": f"/ws/audio?ticket={ticket}", "expires_in": 30}


@app.get("/api/v1/conversations")
async def conversations(p: Principal = Depends(require("conversations:read"))):
    events = [e for e in AUDIT if e.get("org") == p.org_id or e["event"].startswith("session")]
    return {"items": events[-100:]}


@app.get("/api/v1/audit-logs")
async def audit_logs(p: Principal = Depends(require("audit:read"))):
    return {"items": AUDIT[-500:]}


@app.get("/api/v1/agents")
async def agents(p: Principal = Depends(require("agents:read"))):
    return {"items": [{"id": "default", "tools": [t["name"] for t in registry.schemas()]}]}


@app.websocket("/ws/audio")
async def ws_audio(ws: WebSocket, ticket: str):
    entry = _tickets.pop(ticket, None)
    if not entry or entry[1] < time.time():
        await ws.close(code=4401)
        return
    principal = entry[0]
    await ws.accept()

    async def send(msg):
        if isinstance(msg, (bytes, bytearray)):
            await ws.send_bytes(bytes(msg))
        else:
            await ws.send_text(json.dumps(msg))

    provider = OpenAIRealtimeProvider(settings) if settings.openai_api_key else FakeProvider()
    session = VoiceSession(principal, provider, registry, send, settings, audit=audit)
    try:
        await session.start()
        while True:
            m = await ws.receive()
            if m.get("type") == "websocket.disconnect":
                break
            if m.get("bytes") is not None:
                raw = m["bytes"]
                seq = struct.unpack(">I", raw[:4])[0]
                await session.on_audio(raw[4:], seq)
            elif m.get("text"):
                await session.on_client_event(json.loads(m["text"]))
    except WebSocketDisconnect:
        pass
    finally:
        await session.close()
