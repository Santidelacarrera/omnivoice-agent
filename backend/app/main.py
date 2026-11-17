import asyncio
import json
import re
import struct
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, Callable

import structlog
from fastapi import Depends, FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, ConfigDict, Field

from app.core.catalog import LANGUAGE_NAMES
from app.core.config import Settings, get_settings
from app.observability.metrics import BARGE_WINDOW, BUDGET_REJECTED, COST_WINDOW, ERRORS, STAGE_WINDOWS, TTFB_WINDOW
from app.orchestration.session import SessionOptions, VoiceSession
from app.realtime.provider import FakeProvider, OpenAIRealtimeProvider, RealtimeProvider
from app.security.auth import Principal, issue_token, require
from app.services.persistence import ApiKeyPrefixTaken, InMemoryPersistence, Persistence, PhoneNumberTaken
from app.services.recordings import RecordingStorage, build_storage
from app.services.retention import InMemoryRetentionBackend, RetentionBackend, RetentionJob
from app.services.state_store import InMemoryStateStore, RedisStateStore, StateStore
from app.tools.handlers import DEMO_ORG, InMemoryRepository, Repository, build_registry

log = structlog.get_logger()

WS_CLOSE_UNAUTHORIZED = 4401
WS_CLOSE_CAPACITY = 4429
WS_CLOSE_TIMEOUT = 4408
IDLE_TIMEOUT_S = 60


def _valid_id(value: str) -> str:
    """Las conversaciones son UUID; un id mal formado es un 404, no un error de base de datos."""
    try:
        uuid.UUID(value)
    except ValueError:
        raise HTTPException(404, "No encontrado") from None
    return value


class CreateSessionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_id: str | None = None
    voice: str | None = Field(default=None, max_length=40)
    language: str | None = Field(default=None, pattern=r"^[a-z]{2}(-[A-Z]{2})?$")
    recording_consent: bool = False  # consentimiento explícito de grabación; por defecto NO


class UpdateAgentBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=2, max_length=80)
    instructions: str | None = Field(default=None, min_length=10, max_length=4000)
    tools: list[str] | None = Field(default=None, max_length=20)
    voice: str | None = Field(default=None, max_length=40)
    language: str | None = Field(default=None, pattern=r"^[a-z]{2}(-[A-Z]{2})?$")


class RollbackBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(ge=1)


class PhoneNumberBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    e164: str = Field(pattern=r"^\+[1-9][0-9]{6,14}$")
    agent_id: str | None = None
    transfer_number: str | None = Field(default=None, pattern=r"^\+[1-9][0-9]{6,14}$")  # operador de esta línea


class CreateApiKeyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=2, max_length=80)
    role: str = Field(default="viewer", pattern=r"^(admin|manager|operator|analyst|viewer)$")
    expires_in_days: int | None = Field(default=None, ge=1, le=730)


class UpdatePhoneBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_id: str | None = None  # null = quitar el agente
    transfer_number: str | None = Field(default=None, pattern=r"^\+[1-9][0-9]{6,14}$")  # null = usar el operador global


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
    twilio: Any = None,
    simulator_llm: Any = None,
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
        dialer = getattr(app.state, "dialer", None)
        if dialer and s.outbound_scheduler_enabled:
            dialer.start()
        yield
        if dialer:
            await dialer.stop()
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
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Permissions-Policy"] = "microphone=(), camera=(), geolocation=()"
        if not request.url.path.startswith("/docs"):
            response.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
        if s.environment == "production":
            response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
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

    async def budget_remaining_s(org_id: str) -> float:
        """Segundos de conversación que le quedan hoy a la organización (inf si no hay tope)."""
        if s.org_daily_budget_minutes <= 0:
            return float("inf")
        return s.org_daily_budget_minutes * 60 - await app.state.store.usage_seconds(org_id)

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
        if len(live) >= s.max_total_sessions:
            BUDGET_REJECTED.labels("global_concurrency").inc()
            raise HTTPException(429, "Capacidad del servicio agotada")
        if await budget_remaining_s(p.org_id) <= 0:
            BUDGET_REJECTED.labels("org_daily_budget").inc()
            raise HTTPException(429, "Presupuesto diario de conversación de la organización agotado")
        voice = body.voice if body else None
        language = body.language if body else None
        if voice and voice not in s.available_voices:
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
            "voices": s.available_voices,
            "languages": [{"code": c, "name": LANGUAGE_NAMES.get(c, c)} for c in s.allowed_languages],
            "recording": {"available": storage is not None and policy["recording_enabled"]},
        }

    # ---------- conversaciones, agentes, métricas, auditoría ----------
    @app.get("/api/v1/conversations")
    async def conversations(limit: int = 50, p: Principal = Depends(authed("conversations:read"))):
        return {"items": await app.state.db.list_conversations(p.org_id, max(1, min(limit, 200)))}

    @app.get("/api/v1/conversations/{conversation_id}")
    async def conversation_detail(conversation_id: str, p: Principal = Depends(authed("conversations:read"))):
        _valid_id(conversation_id)
        conv = await app.state.db.get_conversation(p.org_id, conversation_id)
        if not conv:
            raise HTTPException(404, "Conversación no encontrada")
        return conv

    @app.delete("/api/v1/conversations/{conversation_id}", status_code=204)
    async def delete_conversation(conversation_id: str, p: Principal = Depends(authed("conversations:delete"))):
        _valid_id(conversation_id)
        """Derecho de supresión (Ley 21.719 / RGPD): borra transcripciones, eventos y grabación de una conversación."""
        keys = await app.state.db.delete_conversation(p.org_id, conversation_id)
        if keys is None:
            raise HTTPException(404, "Conversación no encontrada")
        for k in keys:
            if storage is not None:
                try:
                    await storage.delete(k)
                except Exception:  # noqa: BLE001
                    ERRORS.labels("recording_delete").inc()
                    log.warning("recording_delete_failed", key_tail=k[-12:])
        await app.state.db.audit(p.org_id, p.user_id, "conversation.deleted",
                                 {"conversation_id": conversation_id, "recordings": len(keys)})
        return Response(status_code=204)

    @app.get("/api/v1/conversations/{conversation_id}/recording")
    async def conversation_recording(conversation_id: str, p: Principal = Depends(authed("recordings:read"))):
        _valid_id(conversation_id)
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
        _check_tools(body.tools)
        agent = await app.state.db.create_agent(p.org_id, body.name, body.instructions, body.tools, body.voice, body.language)
        await app.state.db.audit(p.org_id, p.user_id, "agent.created", {"agent_id": agent["id"], "tools": body.tools})
        return agent

    class SimulateBody(BaseModel):
        model_config = ConfigDict(extra="forbid")
        turns: list[str] = Field(min_length=1, max_length=10)

    @app.post("/api/v1/agents/{agent_id}/simulate")
    async def simulate_agent(agent_id: str, body: SimulateBody, p: Principal = Depends(authed("agents:write"))):
        """Prueba un agente (también en borrador) con una conversación de texto sobre herramientas de demostración."""
        agent = await _agent_or_404(p.org_id, agent_id)
        if agent["status"] == "archived":
            raise HTTPException(409, "Un agente archivado no se puede probar")
        llm = simulator_llm or getattr(app.state, "simulator_llm", None)
        if llm is None and s.anthropic_api_key:
            from app.realtime.cascade import AnthropicLLM

            llm = app.state.simulator_llm = AnthropicLLM(s)
        if llm is None:
            raise HTTPException(503, "Simulador no configurado (falta ANTHROPIC_API_KEY)")
        if not await app.state.store.hit("simulate", p.org_id, s.simulate_per_minute, 60):
            raise HTTPException(429, "Demasiadas simulaciones; espera un minuto")
        from app.orchestration.simulator import SimulationError, simulate

        try:
            result = await simulate(s, llm, agent, p.org_id, body.turns)
        except SimulationError as exc:
            raise HTTPException(exc.status, exc.message) from None
        await app.state.db.audit(p.org_id, p.user_id, "agent.simulated",
                                 {"agent_id": agent_id, "turns": len(body.turns), "tokens_in": result["usage"]["in"],
                                  "tokens_out": result["usage"]["out"]})  # no se guardan las frases de prueba
        return result

    def _check_tools(tools):
        unknown = set(tools) - {t["name"] for t in registry.schemas()}
        if unknown:
            raise HTTPException(422, f"Herramientas desconocidas: {sorted(unknown)}")

    async def _agent_or_404(org_id: str, agent_id: str):
        a = await app.state.db.get_agent(org_id, _valid_id(agent_id))
        if not a:
            raise HTTPException(404, "Agente no encontrado")
        return a

    @app.get("/api/v1/agents/{agent_id}")
    async def get_agent(agent_id: str, p: Principal = Depends(authed("agents:read"))):
        return await _agent_or_404(p.org_id, agent_id)

    @app.patch("/api/v1/agents/{agent_id}")
    async def update_agent(agent_id: str, body: UpdateAgentBody, p: Principal = Depends(authed("agents:write"))):
        await _agent_or_404(p.org_id, agent_id)
        changes = body.model_dump(exclude_unset=True, exclude_none=True)
        if not changes:
            raise HTTPException(422, "Sin cambios")
        if "tools" in changes:
            _check_tools(changes["tools"])
        agent = await app.state.db.update_agent(p.org_id, agent_id, changes)
        if agent is None:
            raise HTTPException(409, "Un agente archivado no se puede editar")
        await app.state.db.audit(p.org_id, p.user_id, "agent.updated",
                                 {"agent_id": agent_id, "version": agent["version"], "fields": sorted(changes)})
        return agent

    @app.get("/api/v1/agents/{agent_id}/versions")
    async def agent_versions(agent_id: str, p: Principal = Depends(authed("agents:read"))):
        versions = await app.state.db.list_agent_versions(p.org_id, _valid_id(agent_id))
        if versions is None:
            raise HTTPException(404, "Agente no encontrado")
        return {"items": versions}

    @app.post("/api/v1/agents/{agent_id}/rollback")
    async def rollback_agent(agent_id: str, body: RollbackBody, p: Principal = Depends(authed("agents:publish"))):
        await _agent_or_404(p.org_id, agent_id)
        agent = await app.state.db.rollback_agent(p.org_id, agent_id, body.version)
        if agent is None:
            raise HTTPException(404, "Versión no encontrada o agente archivado")
        await app.state.db.audit(p.org_id, p.user_id, "agent.rolled_back",
                                 {"agent_id": agent_id, "to": body.version, "new_version": agent["version"]})
        return agent

    def _status_route(name: str, status: str, action: str):
        async def handler(agent_id: str, p: Principal = Depends(authed("agents:publish"))):
            await _agent_or_404(p.org_id, agent_id)
            agent = await app.state.db.set_agent_status(p.org_id, agent_id, status)
            await app.state.db.audit(p.org_id, p.user_id, action, {"agent_id": agent_id})
            return agent

        handler.__name__ = name
        app.post(f"/api/v1/agents/{{agent_id}}/{name}")(handler)

    _status_route("publish", "published", "agent.published")
    _status_route("archive", "archived", "agent.archived")

    def _humans_only(p: Principal) -> None:
        """Una clave de API no gestiona claves: si se filtra, no puede crear otras para perpetuar el acceso."""
        if p.user_id.startswith("apikey:"):
            raise HTTPException(403, "Las claves de API no pueden gestionar claves")

    @app.post("/api/v1/api-keys", status_code=201)
    async def create_api_key(body: CreateApiKeyBody, p: Principal = Depends(authed("apikeys:write"))):
        """Crea una clave para integraciones. La clave completa se devuelve **una sola vez**; después solo se ve su prefijo."""
        _humans_only(p)
        from datetime import datetime, timedelta, timezone

        from app.security import apikeys
        from app.security.auth import ROLE_PERMISSIONS

        # Nadie entrega más poder del que tiene: una clave no puede superar los permisos de quien la crea.
        if not ROLE_PERMISSIONS[body.role] <= ROLE_PERMISSIONS.get(p.role, set()):
            raise HTTPException(403, "No puedes crear una clave con más permisos que tu propio rol")
        expires = datetime.now(timezone.utc) + timedelta(days=body.expires_in_days) if body.expires_in_days else None
        for _ in range(5):
            key, prefix, key_hash = apikeys.generate()
            try:
                created = await app.state.db.create_api_key(p.org_id, body.name, prefix, key_hash, body.role, p.user_id, expires)
                break
            except ApiKeyPrefixTaken:
                continue
        else:
            raise HTTPException(503, "No se pudo generar la clave; inténtalo de nuevo")
        await app.state.db.audit(p.org_id, p.user_id, "apikey.created", {"key_id": created["id"], "role": body.role, "prefix": prefix})
        return {**created, "key": key, "warning": "Guarda esta clave ahora: no se vuelve a mostrar."}

    @app.get("/api/v1/api-keys")
    async def list_api_keys(p: Principal = Depends(authed("apikeys:write"))):
        _humans_only(p)
        return {"items": await app.state.db.list_api_keys(p.org_id)}

    @app.delete("/api/v1/api-keys/{key_id}", status_code=204)
    async def revoke_api_key(key_id: str, p: Principal = Depends(authed("apikeys:write"))):
        _humans_only(p)
        if not await app.state.db.revoke_api_key(p.org_id, _valid_id(key_id)):
            raise HTTPException(404, "Clave no encontrada")
        await app.state.db.audit(p.org_id, p.user_id, "apikey.revoked", {"key_id": key_id})
        return Response(status_code=204)

    @app.get("/api/v1/phone-numbers")
    async def list_phones(p: Principal = Depends(authed("phones:read"))):
        return {"items": await app.state.db.list_phone_numbers(p.org_id)}

    @app.post("/api/v1/phone-numbers", status_code=201)
    async def add_phone(body: PhoneNumberBody, p: Principal = Depends(authed("phones:write"))):
        if body.agent_id is not None:
            await _agent_or_404(p.org_id, body.agent_id)  # el agente debe ser de la misma organización
        try:
            phone = await app.state.db.add_phone_number(p.org_id, body.e164, body.agent_id, body.transfer_number)
        except PhoneNumberTaken:
            raise HTTPException(409, "Ese número ya está registrado") from None
        await app.state.db.audit(p.org_id, p.user_id, "phone.added", {"phone_id": phone["id"], "agent_id": body.agent_id})
        return phone

    @app.patch("/api/v1/phone-numbers/{phone_id}")
    async def update_phone(phone_id: str, body: UpdatePhoneBody, p: Principal = Depends(authed("phones:write"))):
        changes = body.model_dump(exclude_unset=True)
        if not changes:
            raise HTTPException(422, "Sin cambios")
        if changes.get("agent_id"):
            await _agent_or_404(p.org_id, changes["agent_id"])
        phone = await app.state.db.update_phone_number(p.org_id, _valid_id(phone_id), changes)
        if not phone:
            raise HTTPException(404, "Número no encontrado")
        await app.state.db.audit(p.org_id, p.user_id, "phone.updated", {"phone_id": phone_id, "fields": sorted(changes)})
        return phone

    @app.delete("/api/v1/phone-numbers/{phone_id}", status_code=204)
    async def delete_phone(phone_id: str, p: Principal = Depends(authed("phones:write"))):
        if not await app.state.db.delete_phone_number(p.org_id, _valid_id(phone_id)):
            raise HTTPException(404, "Número no encontrado")
        await app.state.db.audit(p.org_id, p.user_id, "phone.deleted", {"phone_id": phone_id})
        return Response(status_code=204)

    @app.get("/api/v1/metrics")
    async def api_metrics(p: Principal = Depends(authed("metrics:read"))):
        return {
            "active_sessions": await app.state.store.active_sessions(p.org_id),
            "first_audio_ms": TTFB_WINDOW.percentiles(scale=1000),
            "barge_in_ms": BARGE_WINDOW.percentiles(scale=1000),
            "stage_ms": {k: w.percentiles(scale=1000) for k, w in STAGE_WINDOWS.items()},
            "cost": COST_WINDOW.summary(),
            "provider": s.active_provider,
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
        if s.active_provider == "gemini" and s.gemini_api_key:
            from app.realtime.gemini import GeminiLiveProvider

            return GeminiLiveProvider(s)
        if s.active_provider == "cascade" and s.deepgram_api_key and s.anthropic_api_key:
            from app.realtime.cascade import build_cascade

            return build_cascade(s)
        if s.openai_api_key:
            return OpenAIRealtimeProvider(s)
        # Modo simulado: responde una vez por sesión para poder ver la interfaz sin clave ni crédito.
        return FakeProvider(respond_on_audio=True, script=[
            {"type": "transcript_user", "text": "(simulado) Hola, quiero consultar el stock."},
            {"type": "transcript_agent", "text": "Modo simulado: añade OPENAI_API_KEY con crédito en .env para voz real."},
        ])

    @app.websocket("/ws/audio")
    async def ws_audio(ws: WebSocket, ticket: str = ""):
        # Defensa en profundidad contra WebSocket entre sitios: si el navegador envía Origin, debe ser uno permitido.
        origin = ws.headers.get("origin")
        if origin and s.environment != "development" and "*" not in s.cors_origins and origin not in s.cors_origins:
            await ws.close(code=WS_CLOSE_UNAUTHORIZED)
            return
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
        remaining = await budget_remaining_s(principal.org_id)
        if remaining <= 0 or len(live) >= s.max_total_sessions:
            BUDGET_REJECTED.labels("org_daily_budget" if remaining <= 0 else "global_concurrency").inc()
            await ws.close(code=WS_CLOSE_CAPACITY)
            return
        if not await app.state.store.acquire_session(principal.org_id, session_key, s.max_sessions_per_org):
            await ws.close(code=WS_CLOSE_CAPACITY)
            return

        async def send(msg):
            if isinstance(msg, (bytes, bytearray)):
                await ws.send_bytes(bytes(msg))
            else:
                await ws.send_text(json.dumps(msg))

        async def on_close(sess: VoiceSession):
            live.pop(session_key, None)
            try:  # el consumo se carga al cerrar; el tope por sesión (deadline) acota lo que puede pasar entre medias
                await app.state.store.add_usage(principal.org_id, time.monotonic() - sess.metrics.started_at)
            except Exception:  # noqa: BLE001
                log.exception("usage_accounting_failed", session=sess.id)
            await app.state.store.release_session(principal.org_id, session_key)

        session: VoiceSession | None = None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + min(s.max_session_seconds, remaining)  # el presupuesto restante también acota la sesión
        try:
            await ws.accept()
            session = VoiceSession(
                principal, make_provider(), registry, send, s, persistence=app.state.db,
                instructions=agent["instructions"] if agent else "Eres un agente de atención al cliente. Responde breve y usa herramientas para datos reales.",
                allowed_tools=set(agent["tools"]) if agent and agent["tools"] else None,
                on_close=on_close, options=options, recording_storage=storage, provider_factory=make_provider,
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
                    if session._closed:  # el cliente pidió terminar ("end"): no se espera más tráfico
                        break
        except (WebSocketDisconnect, asyncio.TimeoutError):
            pass
        finally:
            if session is not None:
                # shield: si el servidor cancela este handler (cierre, desconexión abrupta) el cierre de la sesión
                # —que guarda la grabación y libera el cupo— debe terminar igualmente.
                try:
                    await asyncio.shield(session.close())  # on_close libera el cupo
                except Exception:  # noqa: BLE001
                    log.exception("session_close_failed", session=session.id)
            else:
                await app.state.store.release_session(principal.org_id, session_key)

    from app.telephony.routes import register_telephony

    register_telephony(app, s, live, make_provider, registry, authed, storage, twilio)

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


_SECRET_PATTERNS = [
    re.compile(r"(?i)([?&](?:key|api_key|token|ticket)=)[^&\s\"']+"),
    re.compile(r"(?i)((?:bearer|token)\s+)[A-Za-z0-9._\-]{8,}"),
    re.compile(r"\b(?:sk-[A-Za-z0-9_\-]{8,}|AIza[0-9A-Za-z_\-]{20,})(?![A-Za-z0-9_\-])"),
]


def redact_secrets(_logger, _method, event_dict):
    """Política de logs: ninguna credencial llega a un log, ni dentro de una excepción o una URL.
    Los transcripts y el audio no se registran en ningún punto del código (hay una prueba que lo verifica)."""
    def clean(v):
        if isinstance(v, str):
            for pat in _SECRET_PATTERNS:
                v = pat.sub(lambda m: (m.group(1) if m.lastindex else "") + "[REDACTED]", v)
            for secret in (s_.openai_api_key, s_.gemini_api_key, s_.deepgram_api_key, s_.anthropic_api_key,
                           s_.twilio_auth_token):
                if secret and len(secret) >= 8:
                    v = v.replace(secret, "[REDACTED]")
        return v
    s_ = get_settings()
    return {k: clean(v) for k, v in event_dict.items()}


structlog.configure(processors=[
    structlog.contextvars.merge_contextvars,
    redact_secrets,
    structlog.processors.add_log_level,
    structlog.processors.TimeStamper(fmt="iso"),
    structlog.processors.JSONRenderer(),
])
app = create_app()
