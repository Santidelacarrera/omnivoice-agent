"""Recuperación ante caídas de dependencias: qué pasa si Redis o PostgreSQL fallan, y que todo vuelve cuando regresan."""
import struct
import time

import pytest
from fastapi.testclient import TestClient
from redis.exceptions import ConnectionError as RedisConnectionError

from app.core.config import Settings
from app.main import create_app
from app.observability.metrics import ERRORS
from app.realtime.provider import FakeProvider
from app.services.state_store import InMemoryStateStore
from tests.test_api import auth

LOUD = struct.pack("<480h", *([12000, -12000] * 240))


def frame(seq):
    return struct.pack(">I", seq) + LOUD


class FlakyStore(InMemoryStateStore):
    """Almacén de estado que se puede «apagar»: mientras está caído toda operación lanza el error que daría Redis."""

    def __init__(self):
        super().__init__()
        self.down = False

    def _check(self):
        if self.down:
            raise RedisConnectionError("Redis caído (simulado)")


def _wrap(name):
    parent = getattr(InMemoryStateStore, name)

    async def method(self, *a, **k):
        self._check()
        return await parent(self, *a, **k)

    method.__name__ = name
    return method


for _name in ("hit", "put_ticket", "take_ticket", "acquire_session", "release_session", "active_sessions", "add_usage", "usage_seconds",
              "put_session_options", "take_session_options", "acquire_lock"):
    setattr(FlakyStore, _name, _wrap(_name))


def client(store=None, **over):
    settings = Settings(environment="test", jwt_secret="t" * 40, vad_backend="energy", **over)
    app = create_app(settings, store=store, provider_factory=lambda: FakeProvider(audio_chunks=3, respond_on_audio=True))
    return TestClient(app)


def errors(kind):
    return ERRORS.labels(kind)._value.get()


# ---------- Redis ----------
def test_redis_outage_gives_503_not_500_and_recovers_by_itself():
    store = FlakyStore()
    c = client(store)
    assert c.get("/api/v1/agents", headers=auth("admin")).status_code == 200
    before = errors("dependency_down")
    store.down = True
    r = c.get("/api/v1/agents", headers=auth("admin"))
    assert r.status_code == 503 and r.headers["retry-after"] == "5" and "no disponible" in r.json()["detail"]
    assert "Traceback" not in r.text and "Redis" not in r.text  # sin detalles internos
    assert c.post("/api/v1/sessions", json={}, headers=auth("customer")).status_code == 503
    assert errors("dependency_down") >= before + 2
    ready = c.get("/readyz")
    assert ready.status_code == 503 and ready.json()["detail"]["state_store"] == "fail"
    assert c.get("/healthz").status_code == 200  # el proceso sigue vivo: solo deja de estar listo
    store.down = False  # vuelve Redis
    assert c.get("/api/v1/agents", headers=auth("admin")).status_code == 200
    assert c.get("/readyz").status_code == 200


def test_a_call_in_progress_survives_a_redis_outage_and_closes_cleanly():
    store = FlakyStore()
    c = client(store)
    c.__enter__()
    sid = c.post("/api/v1/sessions", json={}, headers=auth("customer")).json()["session_id"]
    ticket = c.post(f"/api/v1/sessions/{sid}/connect", headers=auth("customer")).json()["ticket"]
    with c.websocket_connect(f"/ws/audio?ticket={ticket}") as ws:
        for i in range(1, 6):
            ws.send_bytes(frame(i))
        store.down = True  # Redis cae a mitad de la llamada
        for i in range(6, 40):
            ws.send_bytes(frame(i))  # el audio sigue fluyendo
        closed = False
        ws.send_text('{"type":"end"}')
        for _ in range(300):
            m = ws.receive()
            if m.get("text") and '"session.closed"' in m["text"]:
                closed = True
                break
        assert closed  # la sesión terminó de forma ordenada, sin excepción (que el proveedor alcance a responder antes del cierre depende de la velocidad del runner)
    store.down = False
    assert c.get("/api/v1/agents", headers=auth("admin")).status_code == 200


# ---------- PostgreSQL ----------
DEAD_PG = "postgresql+asyncpg://omni_app:x@127.0.0.1:1/omnivoice"  # nadie escucha en el puerto 1


def dead_pg_client():
    s = Settings(environment="test", jwt_secret="t" * 40, vad_backend="energy", persistence_backend="postgres", database_url=DEAD_PG,
                 retention_job_enabled=False, outbound_scheduler_enabled=False, webhook_worker_enabled=False,
                 db_connect_timeout_s=1.0)
    return TestClient(create_app(s, provider_factory=lambda: FakeProvider(audio_chunks=3, respond_on_audio=True)))


def test_postgres_outage_data_endpoints_return_503_and_readiness_fails():
    with dead_pg_client() as c:
        assert c.get("/healthz").status_code == 200
        ready = c.get("/readyz")
        assert ready.status_code == 503 and ready.json()["detail"]["database"] == "fail"
        r = c.get("/api/v1/conversations", headers=auth("admin"))
        assert r.status_code == 503 and "Traceback" not in r.text and "asyncpg" not in r.text and "127.0.0.1" not in r.text
        assert c.get("/api/v1/agents", headers=auth("admin")).status_code == 503


def test_a_voice_call_still_works_while_postgres_is_down():
    """ADR-6: persistir nunca rompe la llamada. Sin base de datos se pierde el historial, no la conversación."""
    with dead_pg_client() as c:
        before, skipped_before, t0 = errors("persistence"), errors("persistence_skipped"), time.time()
        sid = c.post("/api/v1/sessions", json={}, headers=auth("customer"))
        assert sid.status_code == 200, sid.text
        ticket = c.post(f"/api/v1/sessions/{sid.json()['session_id']}/connect", headers=auth("customer")).json()["ticket"]
        with c.websocket_connect(f"/ws/audio?ticket={ticket}") as ws:
            for i in range(1, 30):
                ws.send_bytes(frame(i))
            ws.send_text('{"type":"end"}')
            closed = False
            for _ in range(300):
                m = ws.receive()
                if m.get("text") and '"session.closed"' in m["text"]:
                    closed = True
                    break
        assert closed  # la sesión terminó de forma ordenada aunque no pudo guardar nada
        assert errors("persistence") > before  # y el fallo quedó contado para la alerta FalloDePersistencia
        assert time.time() - t0 < 25, "la llamada no debe quedarse esperando a la base de datos"
        assert errors("persistence_skipped") > skipped_before  # tras unos fallos el cortacircuitos omite sin esperar
        assert c.get("/healthz").json() == {"status": "ok"}


@pytest.mark.parametrize("path", ["/api/v1/usage", "/api/v1/analytics", "/api/v1/calls", "/api/v1/phone-numbers", "/api/v1/audit-logs"])
def test_every_data_endpoint_degrades_to_503_when_postgres_is_down(path):
    with dead_pg_client() as c:
        r = c.get(path, headers=auth("admin"))
        assert r.status_code in (503, 404), (path, r.status_code, r.text[:200])  # 404 si el módulo (telefonía) no está activado
        assert r.status_code != 500


# ---------- cortacircuitos de la persistencia ----------
def test_breaker_opens_after_repeated_failures_skips_then_recovers():
    from app.services.pg_persistence import BREAKER, _Breaker, _safe

    b = _Breaker(threshold=3, window_s=10)
    now = [100.0]
    b.clock = lambda: now[0]
    assert not b.is_open()
    for _ in range(2):
        b.failure()
    assert not b.is_open()  # aún por debajo del umbral
    b.failure()
    assert b.is_open()  # abierto: se omite sin esperar
    now[0] += 9
    assert b.is_open()
    now[0] += 2  # pasó la ventana: deja pasar UNA prueba
    assert not b.is_open() and b.is_open()  # y vuelve a cerrar la ventana si esa prueba falla
    b.success()
    assert not b.is_open() and b.failures == 0

    calls = []

    @_safe
    async def op():
        calls.append(1)
        raise OSError("base caída")

    import asyncio
    BREAKER.success()
    for _ in range(6):
        assert asyncio.run(op()) is None
    assert len(calls) == BREAKER.threshold  # a partir del tercer fallo ya no se intenta
    BREAKER.success()
