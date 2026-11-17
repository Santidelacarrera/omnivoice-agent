"""Etapa 10 contra PostgreSQL real: cola de webhooks (migración 012): cifrado en reposo, RLS, reclamo atómico y reintentos."""
import asyncio
import os
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import Settings, get_settings
from app.main import create_app
from app.security.auth import issue_token
from app.services import webhooks as wh

PG_URL = os.environ.get("TEST_DATABASE_URL")
REDIS_URL = os.environ.get("TEST_REDIS_URL")
ADMIN_URL = os.environ.get("TEST_ADMIN_DATABASE_URL")
DEMO = "00000000-0000-0000-0000-000000000001"
OTHER = "00000000-0000-0000-0000-0000000000ee"
URL = "https://hooks.example.com/pg"

pytestmark = pytest.mark.skipif(not (PG_URL and REDIS_URL), reason="requiere TEST_DATABASE_URL y TEST_REDIS_URL")


def _h(org, role="admin"):
    return {"Authorization": f"Bearer {issue_token('u-wh', org, role)}"}


class Receiver:
    def __init__(self, status=200):
        self.status, self.requests = status, []

    def handler(self, request):
        self.requests.append(request)
        return httpx.Response(self.status)


def _app(rec):
    s = Settings(environment="test", jwt_secret=get_settings().jwt_secret, persistence_backend="postgres", state_backend="redis",
                 database_url=PG_URL, redis_url=REDIS_URL, vad_backend="energy", retention_job_enabled=False, webhook_worker_enabled=False)
    return TestClient(create_app(s, webhook_http=httpx.AsyncClient(transport=httpx.MockTransport(rec.handler)),
                                 webhook_resolver=lambda host: ["93.184.216.34"]))


def _drain(c):
    """Vacía lo pendiente de pruebas anteriores para que esta cuente solo lo suyo."""
    for _ in range(50):
        if c.portal.call(c.app.state.webhooks.tick, datetime.now(timezone.utc) + timedelta(days=365)) == 0:
            return


def test_secret_encrypted_delivery_retry_and_isolation_on_postgres():
    rec = Receiver()
    with _app(rec) as c:
        _drain(c)
        rec.requests.clear()
        hook = c.post("/api/v1/webhooks", headers=_h(DEMO), json={"url": URL, "events": ["call.completed", "webhook.test"]}).json()
        assert hook["secret"].startswith("whsec_")

        async def stored():
            eng = create_async_engine(ADMIN_URL)
            try:
                async with eng.begin() as conn:
                    return (await conn.execute(text("SELECT secret_enc FROM webhook_endpoints WHERE id = :i"), {"i": hook["id"]})).scalar()
            finally:
                await eng.dispose()

        if ADMIN_URL:
            enc = asyncio.run(stored())
            assert hook["secret"] not in enc  # la base solo tiene el cifrado

        assert c.get("/api/v1/webhooks", headers=_h(OTHER)).json()["items"] == []  # RLS
        assert c.post(f"/api/v1/webhooks/{hook['id']}/test", headers=_h(OTHER)).status_code == 404

        assert c.post(f"/api/v1/webhooks/{hook['id']}/test", headers=_h(DEMO)).json() == {"queued": 1}
        assert c.portal.call(c.app.state.webhooks.tick) == 1
        req = rec.requests[0]
        assert wh.verify(hook["secret"], req.headers["x-omnivoice-signature"], req.content.decode())
        d = c.get(f"/api/v1/webhooks/{hook['id']}/deliveries", headers=_h(DEMO)).json()["items"]
        assert d[0]["status"] == "delivered" and d[0]["attempts"] == 1 and d[0]["last_status"] == 200

        # un evento que no está en la suscripción no se encola
        assert c.portal.call(c.app.state.webhooks.emit, DEMO, "tool.executed", {"tool": "x"}) == 0

        # fallo → reintento programado en la base
        rec.status = 503
        c.portal.call(c.app.state.webhooks.emit, DEMO, "call.completed", {"call_id": "c1", "status": "COMPLETED"})
        assert c.portal.call(c.app.state.webhooks.tick) == 1
        latest = c.get(f"/api/v1/webhooks/{hook['id']}/deliveries", headers=_h(DEMO)).json()["items"][0]
        assert latest["status"] == "pending" and latest["attempts"] == 1 and latest["last_error"] == "http_503"
        assert c.portal.call(c.app.state.webhooks.tick) == 0  # aún no vence (30 s)
        rec.status = 200
        assert c.portal.call(c.app.state.webhooks.tick, datetime.now(timezone.utc) + timedelta(seconds=31)) == 1
        assert c.get(f"/api/v1/webhooks/{hook['id']}/deliveries", headers=_h(DEMO)).json()["items"][0]["status"] == "delivered"

        assert c.delete(f"/api/v1/webhooks/{hook['id']}", headers=_h(OTHER)).status_code == 404
        assert c.delete(f"/api/v1/webhooks/{hook['id']}", headers=_h(DEMO)).status_code == 204
        assert c.get(f"/api/v1/webhooks/{hook['id']}/deliveries", headers=_h(DEMO)).status_code == 404  # sus entregas se fueron con él


def test_claim_is_atomic_across_concurrent_workers_and_recovers_stale():
    rec = Receiver()
    with _app(rec) as c:
        _drain(c)
        db = c.app.state.db
        hook = c.post("/api/v1/webhooks", headers=_h(DEMO), json={"url": URL}).json()
        for i in range(6):
            c.portal.call(c.app.state.webhooks.emit, DEMO, "call.completed", {"n": i})
        now = datetime.now(timezone.utc)

        async def race():
            return await asyncio.gather(*(db.claim_due_webhook_deliveries(now + timedelta(seconds=1), 3, 300) for _ in range(4)))

        claimed = [j["id"] for batch in c.portal.call(race) for j in batch]
        assert len(claimed) == len(set(claimed)) == 6  # cada entrega la toma un solo trabajador
        again = c.portal.call(db.claim_due_webhook_deliveries, now + timedelta(seconds=400), 50, 300)
        assert {j["id"] for j in again} == set(claimed)  # lo abandonado "enviando" se recupera
        c.delete(f"/api/v1/webhooks/{hook['id']}", headers=_h(DEMO))


def test_auto_disable_and_purge_on_postgres():
    if not ADMIN_URL:
        pytest.skip("requiere TEST_ADMIN_DATABASE_URL")
    rec = Receiver()
    with _app(rec) as c:
        _drain(c)
        db = c.app.state.db
        hook = c.post("/api/v1/webhooks", headers=_h(DEMO), json={"url": URL}).json()
        for i in range(3):
            c.portal.call(c.app.state.webhooks.emit, DEMO, "call.failed", {"n": i})

        async def admin(sql, **params):
            eng = create_async_engine(ADMIN_URL)
            try:
                async with eng.begin() as conn:
                    return (await conn.execute(text(sql), params)).rowcount
            finally:
                await eng.dispose()

        # las dos últimas fallan, la primera sigue pendiente: aún no hay racha de 2 fallidas seguidas... salvo las dos últimas
        asyncio.run(admin("UPDATE webhook_deliveries SET status = 'failed' WHERE endpoint_id = :e AND id IN "
                          "(SELECT id FROM webhook_deliveries WHERE endpoint_id = :e ORDER BY id DESC LIMIT 2)", e=hook["id"]))
        assert c.portal.call(db.disable_webhook_if_failing, DEMO, hook["id"], 3) is False  # la más antigua no falló: no hay racha de 3
        assert c.portal.call(db.disable_webhook_if_failing, DEMO, hook["id"], 2) is True
        assert c.get("/api/v1/webhooks", headers=_h(DEMO)).json()["items"][0]["active"] is False
        assert c.portal.call(db.disable_webhook_if_failing, DEMO, hook["id"], 2) is False  # ya desactivado

        # purga: solo lo terminado y antiguo
        asyncio.run(admin("UPDATE webhook_deliveries SET created_at = now() - interval '30 days' WHERE endpoint_id = :e AND status = 'failed'", e=hook["id"]))
        removed = c.portal.call(db.purge_webhook_deliveries, datetime.now(timezone.utc) - timedelta(days=14))
        assert removed >= 2
        left = c.get(f"/api/v1/webhooks/{hook['id']}/deliveries", headers=_h(DEMO)).json()["items"]
        assert [d["status"] for d in left] == ["pending"]  # la pendiente sobrevive
        c.delete(f"/api/v1/webhooks/{hook['id']}", headers=_h(DEMO))
