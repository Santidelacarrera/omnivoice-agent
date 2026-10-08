import asyncio
import json
import struct
import uuid
from contextlib import asynccontextmanager
from typing import Callable

import structlog
from fastapi import Depends, FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, ConfigDict, Field

from app.core.catalog import LANGUAGE_NAMES
from app.core.config import Settings, get_settings
from app.observability.metrics import BARGE_WINDOW, ERRORS, TTFB_WINDOW
from app.orchestration.session import SessionOptions, VoiceSession
from app.realtime.provider import FakeProvider, OpenAIRealtimeProvider, RealtimeProvider
from app.security.auth import Principal, issue_token, require
from app.services.persistence import InMemoryPersistence, Persistence
from app.services.recordings import RecordingStorage, build_storage
from app.services.retention import InMemoryRetentionBackend, RetentionBackend, RetentionJob
from app.services.state_store import InMemoryStateStore, RedisStateStore, StateStore
from app.tools.handlers import DEMO_ORG, InMemoryRepository, Repository, build_registry

log = structlog.get_logger()

WS_CLOSE_UNAUTHORIZED = 4401
WS_CLOSE_CAPACITY = 4429
WS_CLOSE_TIMEOUT = 4408
IDLE_TIMEOUT_S = 60


class CreateSessionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_id: str | None = None
    voice: str | None = Field(default=None, max_length=40)
    language: str | None = Field(default=None, pattern=r"^[a-z]{2}(-[A-Z]{2})?$")
    recording_consent: bool = False  # consentimiento explícito de grabación; por defecto NO


class CreateAgentBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=2, max_length=80)
    instructions: str = Field(min_length=10, max_length=4000)
    tools: list[str] = Field(default_factory=list, max_length=20)
    voice: str | None = Field(default=None, max_length=40)
    language: str = Field(default="es", pattern=r"^[a-z]{2}(-[A-Z]{2})?$")


class DevLogin(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: str = Field(min_length=1, max_length=64, default="demo-user")
    org_id: str = Field(min_length=1, max_length=64, default=DEMO_ORG)
    role: str = Field(default="customer", pattern=r"^(admin|operator|customer)$")


def create_app(
    settings: Settings | None = None,
    persistence: Persistence | None = None,
    store: StateStore | None = None,
    repository: Repository | None = None,
    provider_factory: Callable[[], RealtimeProvider] | None = None,
    recording_storage: RecordingStorage | None = None,
    retention_backend: RetentionBackend | None = None,
) -> FastAPI:
    s = settings or get_settings()
    live: dict[str, VoiceSession] = {}
    storage: RecordingStorage | None = recording_storage if recording_storage is not None else build_storage(s)
    retention_job: RetentionJob | None = None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal retention_job
        redis_client = None
        if persistence is None and s.persistence_backend == "postgres":
            from app.database.engine import init_engine

            from app.services.pg_persistence import PostgresPersistence

            init_engine(s.database_url)
            app.state.db = PostgresPersistence()
            app.state.repo = repository or _postgres_repo()
        if store is None and s.state_backend == "redis":
            import redis.asyncio as aioredis

            redis_client = aioredis.from_url(s.redis_url, decode_responses=True)
            app.state.store = RedisStateStore(redis_client)
        if s.retention_job_enabled:
            backend = retention_backend
            if backend is None:
                if s.persistence_backend == "postgres" and persistence is None:
                    from app.services.retention_pg import PostgresRetentionBackend

                    backend = PostgresRetentionBackend()
                elif isinstance(app.state.db, InMemoryPersistence):
                    backend = InMemoryRetentionBackend(app.state.db, s.memory_retention_days)
            if backend is not None:
                retention_job = RetentionJob(backend, storage, lambda *a: app.state.db.audit(*a),
                                             lambda name, ttl: app.state.store.acquire_lock(name, ttl),
                                             s.retention_interval_seconds, s.retention_batch_size)
                app.state.retention = retention_job
                retention_job.start()
        yield
        if retention_job:
            await retention_job.stop()
        # Cierre ordenado: finaliza sesiones vivas para no perder conversaciones ni consumo.
        await asyncio.gather(*(sess.close() for sess in list(live.values())), return_exceptions=True)
        if redis_client:
            await redis_client.aclose()
        if s.persistence_backend == "postgres" and persistence is None:
            from app.database.engine import dispose_engine

            await dispose_engine()

    app = FastAPI(title=s.app_name, version="1.0.0", lifespan=lifespan,
                  docs_url=None if s.environment == "production" else "/docs")
    app.state.db = persistence or InMemoryPersistence()
    app.state.store = store or InMemoryStateStore()
    app.state.repo = repository or InMemoryRepository()
    registry = build_registry(_RepoProxy(app))

    app.add_middleware(CORSMiddleware, allow_origins=s.cors_origins, allow_methods=["GET", "POST"],
                       allow_headers=["Authorization", "Content-Type"], allow_credentials=True)

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=rid)
        response: Response = await call_next(request)
        response.headers["X-Request-ID"] = rid
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    def authed(permission: str):
        base = require(permission)

        async def dep(p: Principal = Depends(base)) -> Principal:
            if not await app.state.store.hit("api", f"{p.org_id}:{p.user_id}", s.rate_limit_api_per_min, 60):
                raise HTTPException(429, "Demasiadas peticiones")
            return p

        return dep

    # ---------- salud y métricas ----------
    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz():
        checks: dict[str, str] = {}
        try:
            await app.state.db.usage_summary(DEMO_ORG)
            checks["database"] = "ok"
        except Exception:  # noqa: BLE001
            checks["database"] = "fail"
        try:
            await app.state.store.active_sessions(DEMO_ORG)
            checks["state_store"] = "ok"
        except Exception:  # noqa: BLE001
            checks["state_store"] = "fail"
        if "fail" in checks.values():
            raise HTTPException(503, checks)
        return {"status": "ready", **checks}

    @app.get("/metrics")
    async def prom_metrics():
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    # ---------- auth de desarrollo ----------
    if s.environment != "production":

        @app.post("/api/v1/auth/dev-token")
        async def dev_token(body: DevLogin):
            return {"token": issue_token(body.user_id, body.org_id, body.role)}

    # ---------- sesiones ----------
    @app.post("/api/v1/sessions")
    async def create_session(body: CreateSessionBody | None = None, p: Principal = Depends(authed("session:create"))):
        if not await app.state.store.hit("create_session", f"{p.org_id}:{p.user_id}", s.rate_limit_sessions_per_min, 60):
            raise HTTPException(429, "Límite de sesiones por minuto alcanzado")
        agent_id = body.agent_id if body else None
        if agent_id and not await app.state.db.get_agent(p.org_id, agent_id):
            raise HTTPException(404, "Agente no encontrado")
        if await app.state.store.active_sessions(p.org_id) >= s.max_sessions_per_org:
            raise HTTPException(429, "Capacidad de la organización agotada")
        voice = body.voice if body else None
        language = body.language if body else None
        if voice and voice not in s.allowed_voices:
            raise HTTPException(422, f"Voz no disponible: {voice}")
        if language and language.split("-")[0] not in s.allowed_languages:
            raise HTTPException(422, f"Idioma no disponible: {language}")
        wants_recording = bool(body and body.recording_consent)
        recording = wants_recording and storage is not None and (await app.state.db.org_policy(p.org_id))["recording_enabled"]
        session_id = uuid.uuid4().hex
        await app.state.store.put_ticket(f"sess:{session_id}", p, 300, agent_id)
        await app.state.store.put_session_options(
            f"sess:{session_id}", {"recording_consent": recording, "voice": voice, "language": language}, 300)
        await app.state.db.audit(p.org_id, p.user_id, "session.created",
                                 {"session_id": session_id, "agent_id": agent_id, "voice": voice, "language": language,
                                  "recording_requested": wants_recording, "recording_granted": recording})
        return {"session_id": session_id, "expires_in": 300, "recording": recording, "voice": voice, "language": language}

    @app.post("/api/v1/sessions/{session_id}/connect")
    async def connect_session(session_id: str, p: Principal = Depends(authed("session:create"))):
        entry = await app.state.store.take_ticket(f"sess:{session_id}")
        if not entry or entry[0].org_id != p.org_id or entry[0].user_id != p.user_id:
            raise HTTPException(404, "Sesión no encontrada o expirada")
        options = await app.state.store.take_session_options(f"sess:{session_id}")
        ticket = uuid.uuid4().hex
        # Ticket WS de un solo uso y vida corta: el JWT nunca viaja en la URL.
        await app.state.store.put_ticket(ticket, p, s.ws_ticket_ttl_seconds, entry[1])
        await app.state.store.put_session_options(ticket, options, s.ws_ticket_ttl_seconds)
        return {"ticket": ticket, "ws_path": f"/ws/audio?ticket={ticket}", "expires_in": s.ws_ticket_ttl_seconds,
                "audio": {"format": "pcm16", "sample_rate": s.sample_rate, "channels": 1, "frame_ms": 20}}

    @app.get("/api/v1/catalog")
    async def catalog(p: Principal = Depends(authed("session:create"))):
        policy = await app.state.db.org_policy(p.org_id)
        return {
            "voices": s.allowed_voices,
            "languages": [{"code": c, "name": LANGUAGE_NAMES.get(c, c)} for c in s.allowed_languages],
            "recording": {"available": storage is not None and policy["recording_enabled"]},
        }

    # ---------- conversaciones, agentes, métricas, auditoría ----------
    @app.get("/api/v1/conversations")
    async def conversations(limit: int = 50, p: Principal = Depends(authed("conversations:read"))):
        return {"items": await app.state.db.list_conversations(p.org_id, max(1, min(limit, 200)))}

    @app.get("/api/v1/conversations/{conversation_id}")
    async def conversation_detail(conversation_id: str, p: Principal = Depends(authed("conversations:read"))):
        conv = await app.state.db.get_conversation(p.org_id, conversation_id)
        if not conv:
            raise HTTPException(404, "Conversación no encontrada")
        return conv

    @app.get("/api/v1/conversations/{conversation_id}/recording")
    async def conversation_recording(conversation_id: str, p: Principal = Depends(authed("recordings:read"))):
        rec = await app.state.db.get_recording(p.org_id, conversation_id)
        if not rec or storage is None:
            raise HTTPException(404, "Esta conversación no tiene grabación")
        await app.state.db.audit(p.org_id, p.user_id, "recording.accessed", {"conversation_id": conversation_id})
        url = await storage.presigned_url(rec["storage_key"], s.recording_url_ttl_seconds)
        if url:
            return {"url": url, "expires_in": s.recording_url_ttl_seconds, "duration_s": rec["duration_s"]}
        data = await storage.get(rec["storage_key"])
        if data is None:
            raise HTTPException(404, "Grabación no disponible")
        return Response(data, media_type="audio/wav", headers={"Content-Disposition": f'attachment; filename="{conversation_id}.wav"'})

    @app.get("/api/v1/agents")
    async def list_agents(p: Principal = Depends(authed("agents:read"))):
        return {"items": await app.state.db.list_agents(p.org_id), "available_tools": [t["name"] for t in registry.schemas()]}

    @app.post("/api/v1/agents", status_code=201)
    async def create_agent(body: CreateAgentBody, p: Principal = Depends(authed("agents:write"))):
        known = {t["name"] for t in registry.schemas()}
        unknown = set(body.tools) - known
        if unknown:
            raise HTTPException(422, f"Herramientas desconocidas: {sorted(unknown)}")
        agent = await app.state.db.create_agent(p.org_id, body.name, body.instructions, body.tools, body.voice, body.language)
        await app.state.db.audit(p.org_id, p.user_id, "agent.created", {"agent_id": agent["id"], "tools": body.tools})
        return agent

    @app.get("/api/v1/metrics")
    async def api_metrics(p: Principal = Depends(authed("metrics:read"))):
        return {
            "active_sessions": await app.state.store.active_sessions(p.org_id),
            "first_audio_ms": TTFB_WINDOW.percentiles(scale=1000),
            "barge_in_ms": BARGE_WINDOW.percentiles(scale=1000),
            "usage": await app.state.db.usage_summary(p.org_id),
            "note": "Percentiles del proceso actual sobre las últimas muestras; para series históricas usa Prometheus.",
        }

    @app.get("/api/v1/audit-logs")
    async def audit_logs(limit: int = 200, p: Principal = Depends(authed("audit:read"))):
        return {"items": await app.state.db.list_audit(p.org_id, max(1, min(limit, 1000)))}

    # ---------- WebSocket de audio ----------
    def make_provider() -> RealtimeProvider:
        if provider_factory:
            return provider_factory()
        if s.openai_api_key:
            return OpenAIRealtimeProvider(s)
        # Modo simulado: responde una vez por sesión para poder ver la interfaz sin clave ni crédito.
        return FakeProvider(respond_on_audio=True, script=[
            {"type": "transcript_user", "text": "(simulado) Hola, quiero consultar el stock."},
            {"type": "transcript_agent", "text": "Modo simulado: añade OPENAI_API_KEY con crédito en .env para voz real."},
        ])

    @app.websocket("/ws/audio")
    async def ws_audio(ws: WebSocket, ticket: str = ""):
        entry = await app.state.store.take_ticket(ticket)
        if not entry:
            await ws.close(code=WS_CLOSE_UNAUTHORIZED)
            return
        principal, agent_id = entry
        agent = await app.state.db.get_agent(principal.org_id, agent_id) if agent_id else None
        opts = await app.state.store.take_session_options(ticket)
        options = SessionOptions(
            recording_consent=bool(opts.get("recording_consent")),
            voice=opts.get("voice") or (agent or {}).get("voice"),
            language=opts.get("language") or (agent or {}).get("language"))
        session_key = uuid.uuid4().hex
        if not await app.state.store.acquire_session(principal.org_id, session_key, s.max_sessions_per_org):
            await ws.close(code=WS_CLOSE_CAPACITY)
            return

        async def send(msg):
            if isinstance(msg, (bytes, bytearray)):
                await ws.send_bytes(bytes(msg))
            else:
                await ws.send_text(json.dumps(msg))

        async def on_close(_sess: VoiceSession):
            live.pop(session_key, None)
            await app.state.store.release_session(principal.org_id, session_key)

        session: VoiceSession | None = None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + s.max_session_seconds
        try:
            await ws.accept()
            session = VoiceSession(
                principal, make_provider(), registry, send, s, persistence=app.state.db,
                instructions=agent["instructions"] if agent else "Eres un agente de atención al cliente. Responde breve y usa herramientas para datos reales.",
                allowed_tools=set(agent["tools"]) if agent and agent["tools"] else None,
                on_close=on_close, options=options, recording_storage=storage,
                recording_allowed=options.recording_consent,  # ya validado contra la política en create_session
            )
            live[session_key] = session
            try:
                await session.start()
            except Exception:  # noqa: BLE001 - p. ej. el proveedor rechaza la conexión
                log.exception("session_start_failed", session_key=session_key)
                ERRORS.labels("provider_connect").inc()
                await ws.send_text(json.dumps({"type": "error", "message": "provider_unavailable"}))
                await ws.close(code=1011)
                return
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    await ws.close(code=WS_CLOSE_TIMEOUT)
                    break
                m = await asyncio.wait_for(ws.receive(), timeout=min(remaining, IDLE_TIMEOUT_S))
                if m.get("type") == "websocket.disconnect":
                    break
                raw = m.get("bytes")
                if raw is not None:
                    if len(raw) < 6 or len(raw) > s.max_ws_frame_bytes:
                        continue  # frame inválido o sobredimensionado: se descarta sin romper la sesión
                    await session.on_audio(raw[4:], struct.unpack(">I", raw[:4])[0])
                elif m.get("text"):
                    if len(m["text"]) > 2048:
                        continue
                    try:
                        await session.on_client_event(json.loads(m["text"]))
                    except json.JSONDecodeError:
                        continue
        except (WebSocketDisconnect, asyncio.TimeoutError):
            pass
        finally:
            if session is not None:
                await session.close()  # on_close libera el cupo
            else:
                await app.state.store.release_session(principal.org_id, session_key)

    return app


class _RepoProxy:
    """Resuelve el repositorio en cada llamada para soportar el cambio de backend en lifespan."""

    def __init__(self, app: FastAPI) -> None:
        self._app = app

    def __getattr__(self, name: str):
        return getattr(self._app.state.repo, name)


def _postgres_repo() -> Repository:
    from app.database.repository import PostgresRepository

    return PostgresRepository()


structlog.configure(processors=[
    structlog.contextvars.merge_contextvars,
    structlog.processors.add_log_level,
    structlog.processors.TimeStamper(fmt="iso"),
    structlog.processors.JSONRenderer(),
])
app = create_app()
