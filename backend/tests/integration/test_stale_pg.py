"""Migración 014 contra PostgreSQL real: búsqueda global de llamadas abandonadas y su cierre por el vigilante."""
import asyncio
import os
import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import Settings, get_settings
from app.main import create_app
from app.security.auth import issue_token
from tests.integration.test_calls_pg import FakeTwilio

PG_URL = os.environ.get("TEST_DATABASE_URL")
REDIS_URL = os.environ.get("TEST_REDIS_URL")
ADMIN_URL = os.environ.get("TEST_ADMIN_DATABASE_URL")
DEMO = "00000000-0000-0000-0000-000000000001"

pytestmark = pytest.mark.skipif(not (PG_URL and REDIS_URL and ADMIN_URL), reason="requiere TEST_DATABASE_URL, TEST_REDIS_URL y TEST_ADMIN_DATABASE_URL")


def test_stale_calls_are_found_globally_and_closed_by_the_reaper():
    s = Settings(environment="test", jwt_secret=get_settings().jwt_secret, persistence_backend="postgres", state_backend="redis",
                 database_url=PG_URL, redis_url=REDIS_URL, vad_backend="energy", retention_job_enabled=False,
                 telephony_provider="twilio", twilio_account_sid="AC1", twilio_auth_token="tok", twilio_from_number="+34910000000",
                 telephony_public_url="https://voz.example.com", outbound_scheduler_enabled=False)
    tw = FakeTwilio()
    with TestClient(create_app(s, twilio=tw)) as c:
        db, dialer = c.app.state.db, c.app.state.dialer
        ringing, live, fresh, done = ("CA" + uuid.uuid4().hex for _ in range(4))
        for sid, final in ((ringing, "RINGING"), (live, "CONNECTED"), (fresh, "RINGING"), (done, "COMPLETED")):
            assert c.portal.call(db.upsert_call, DEMO, "twilio", sid, "inbound", None, "+34910000000", "h")
            c.portal.call(db.apply_call_event, DEMO, "twilio", sid, final, "k1", {})

        async def age(sid, seconds):
            eng = create_async_engine(ADMIN_URL)
            try:
                async with eng.begin() as conn:
                    await conn.execute(text("UPDATE call_events SET ts = ts - make_interval(secs => :s) WHERE call_id = "
                                            "(SELECT id FROM calls WHERE provider_call_sid = :sid)"), {"s": seconds, "sid": sid})
            finally:
                await eng.dispose()

        asyncio.run(age(ringing, 1000))  # > 900 s sin contestar
        asyncio.run(age(live, 11000))  # > 3 h en curso
        asyncio.run(age(done, 99999))  # terminada: nunca es abandonada

        found = c.portal.call(db.find_stale_calls, datetime.now(timezone.utc), 900, 10800)
        sids = {x["sid"]: x for x in found}
        assert ringing in sids and live in sids and fresh not in sids and done not in sids
        assert sids[ringing]["org_id"] == DEMO and sids[live]["status"] == "CONNECTED"  # ve todas las organizaciones

        assert c.portal.call(dialer.reap) >= 2
        calls = {x["sid"]: x["status"] for x in c.get("/api/v1/calls?limit=200", headers={
            "Authorization": "Bearer " + issue_token("u", DEMO, "analyst")}).json()["items"]}
        assert calls[ringing] == "FAILED" and calls[live] == "FAILED" and calls[fresh] == "RINGING" and calls[done] == "COMPLETED"
        after = {x["sid"] for x in c.portal.call(db.find_stale_calls, datetime.now(timezone.utc), 900, 10800)}
        assert ringing not in after and live not in after  # ya no vuelven a aparecer
