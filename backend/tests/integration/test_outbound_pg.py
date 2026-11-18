"""Etapa 3 contra PostgreSQL real: trabajos salientes (migración 008): duplicados, reclamo atómico, reintentos, exclusión y RLS."""
import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import Settings, get_settings
from app.main import create_app
from app.security.auth import issue_token
from app.telephony import outbound as ob
from tests.integration.test_calls_pg import FakeTwilio, _status

PG_URL = os.environ.get("TEST_DATABASE_URL")
REDIS_URL = os.environ.get("TEST_REDIS_URL")
ADMIN_URL = os.environ.get("TEST_ADMIN_DATABASE_URL")
DEMO = "00000000-0000-0000-0000-000000000001"
OTHER = "00000000-0000-0000-0000-0000000000ee"

pytestmark = pytest.mark.skipif(not (PG_URL and REDIS_URL), reason="requiere TEST_DATABASE_URL y TEST_REDIS_URL")


def _h(org, role="admin"):
    return {"Authorization": f"Bearer {issue_token('u-out', org, role)}"}


def _settings(**over):
    base = dict(environment="test", jwt_secret=get_settings().jwt_secret, persistence_backend="postgres", state_backend="redis",
                database_url=PG_URL, redis_url=REDIS_URL, vad_backend="energy", retention_job_enabled=False,
                telephony_provider="twilio", twilio_account_sid="AC1", twilio_auth_token="tok", twilio_from_number="+34910000000",
                telephony_public_url="https://voz.example.com", outbound_window_start="00:00", outbound_window_end="23:59",
                outbound_days="0,1,2,3,4,5,6", outbound_scheduler_enabled=False)
    return Settings(**{**base, **over})


def _number():
    return "+5698" + str(uuid.uuid4().int)[:7]


def _body(to, **kw):
    return {"to": to, "consent_basis": "existing_customer", **kw}


async def _get(c, org, jid):
    return await c.app.state.db.get_outbound_job(org, jid)


async def _tick(c, now):
    return await c.app.state.dialer.tick(now)


async def _cancel_all(ids):
    if not ADMIN_URL:
        return
    eng = create_async_engine(ADMIN_URL)
    try:
        async with eng.begin() as conn:
            for i in ids:
                await conn.execute(text("UPDATE outbound_jobs SET status = 'CANCELLED', to_number = NULL, finished_at = now() WHERE id = :i"), {"i": i})
    finally:
        await eng.dispose()


def test_job_lifecycle_duplicates_retries_and_number_erasure_on_postgres():
    tw = FakeTwilio()
    to = _number()
    with TestClient(create_app(_settings(), twilio=tw)) as c:
        job = c.post("/api/v1/outbound-calls", headers=_h(DEMO), json=_body(to, max_attempts=2)).json()
        assert job["status"] == "DIALING" and job["attempts"] == 1 and tw.calls[0][0] == to

        # duplicado mientras hay un trabajo activo, e idempotencia por clave
        assert c.post("/api/v1/outbound-calls", headers=_h(DEMO), json=_body(to)).status_code == 409
        key = "clave-" + uuid.uuid4().hex[:8]
        other_to = _number()
        later = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        a = c.post("/api/v1/outbound-calls", headers=_h(DEMO), json=_body(other_to, idempotency_key=key, scheduled_at=later)).json()
        b = c.post("/api/v1/outbound-calls", headers=_h(DEMO), json=_body(other_to, idempotency_key=key)).json()
        assert a["id"] == b["id"] and b["replayed"] is True

        sid = job["last_call_sid"]
        _status(c, sid, "ringing", "1")
        _status(c, sid, "no-answer", "2")
        j = c.portal.call(_get, c, DEMO, job["id"])
        assert j["status"] == "SCHEDULED" and j["last_result"] == "NO_ANSWER" and j["to_number"] == to

        # reintento: el planificador lo toma cuando vence
        assert c.portal.call(_tick, c, datetime.now(timezone.utc) + timedelta(minutes=16)) >= 1
        sid2 = c.portal.call(_get, c, DEMO, job["id"])["last_call_sid"]  # la llamada de ESTE trabajo (no cualquiera en cola de la organización)
        assert sid2 and sid2 != sid
        _status(c, sid2, "busy", "1")
        j = c.portal.call(_get, c, DEMO, job["id"])
        assert j["status"] == "EXHAUSTED" and j["attempts"] == 2 and j["to_number"] is None

        # lo ya terminado permite un trabajo nuevo para el mismo número
        assert c.post("/api/v1/outbound-calls", headers=_h(DEMO), json=_body(to)).status_code == 202
        asyncio.run(_cancel_all([a["id"]]))


def test_do_not_call_and_isolation_on_postgres():
    tw = FakeTwilio()
    to = _number()
    with TestClient(create_app(_settings(), twilio=tw)) as c:
        far = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
        job = c.post("/api/v1/outbound-calls", headers=_h(DEMO), json=_body(to, scheduled_at=far)).json()
        assert job["status"] == "SCHEDULED" and not tw.calls
        d = c.post("/api/v1/do-not-call", headers=_h(DEMO), json={"number": to, "reason": "pidió no ser llamado"}).json()
        assert to not in str(d)
        assert c.get(f"/api/v1/outbound-calls/{job['id']}", headers=_h(DEMO)).json()["status"] == "CANCELLED"
        assert c.post("/api/v1/outbound-calls", headers=_h(DEMO), json=_body(to)).status_code == 403
        assert c.get(f"/api/v1/outbound-calls/{job['id']}", headers=_h(OTHER)).status_code == 404
        assert c.get("/api/v1/do-not-call", headers=_h(OTHER)).json()["items"] == []
        assert c.delete(f"/api/v1/do-not-call/{d['id']}", headers=_h(OTHER)).status_code == 404
        assert c.delete(f"/api/v1/do-not-call/{d['id']}", headers=_h(DEMO)).status_code == 204


def test_claim_is_atomic_across_concurrent_schedulers_and_recovers_stale_claims():
    """Cuatro planificadores simultáneos nunca toman el mismo trabajo (SKIP LOCKED); uno abandonado 'marcando' se recupera."""
    with TestClient(create_app(_settings(), twilio=FakeTwilio())) as c:
        db = c.app.state.db
        now = datetime.now(timezone.utc)
        ids = []
        for _ in range(6):
            n = _number()
            r = c.portal.call(lambda n=n: db.create_outbound_job(
                DEMO, number_hash=ob.number_hash(DEMO, n), to_number=n, from_number="+34910000000", timezone="America/Santiago",
                consent_basis="existing_customer", consent_ref=None, idempotency_key=None, max_attempts=3,
                next_attempt_at=now - timedelta(minutes=1), created_by="t"))
            ids.append(r["job"]["id"])

        async def race():
            return await asyncio.gather(*(db.claim_due_outbound_jobs(now, 3, 600) for _ in range(4)))

        claimed = [j["id"] for batch in c.portal.call(race) for j in batch]
        assert len(claimed) == len(set(claimed))  # nadie tomó el mismo trabajo dos veces
        mine = {i for i in claimed if i in ids}
        assert mine  # reclamó al menos parte de los suyos
        again = {j["id"] for j in c.portal.call(db.claim_due_outbound_jobs, now + timedelta(seconds=601), 50, 600)}
        assert mine <= again  # pasado el plazo, lo abandonado vuelve a estar disponible
        asyncio.run(_cancel_all(ids))
