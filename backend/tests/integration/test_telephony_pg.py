"""Etapa 3 contra PostgreSQL real: resolución número → organización/agente (migración 006, SECURITY DEFINER)."""
import asyncio
import os
import uuid
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import Settings, get_settings
from app.main import create_app
from app.security.auth import issue_token
from app.telephony.twilio import compute_signature

PG_URL = os.environ.get("TEST_DATABASE_URL")
REDIS_URL = os.environ.get("TEST_REDIS_URL")
PUBLIC = "https://voz.example.com"
DEMO = "00000000-0000-0000-0000-000000000001"
OTHER = "00000000-0000-0000-0000-0000000000ee"

pytestmark = pytest.mark.skipif(not (PG_URL and REDIS_URL), reason="requiere TEST_DATABASE_URL y TEST_REDIS_URL")


class FakeTwilio:
    async def create_call(self, *a, **k): return {"sid": "x"}
    async def redirect_call(self, *a, **k): return None


def _h(org):
    return {"Authorization": f"Bearer {issue_token('u-tel', org, 'admin')}"}


def _call(c, to):
    params = {"CallSid": "CA1", "From": "+34600000000", "To": to}
    return c.post("/telephony/voice", content=urlencode(params), headers={
        "content-type": "application/x-www-form-urlencoded",
        "x-twilio-signature": compute_signature("tok", f"{PUBLIC}/telephony/voice", params)})


def test_number_resolves_org_and_agent_on_postgres():
    s = Settings(environment="test", jwt_secret=get_settings().jwt_secret, persistence_backend="postgres", state_backend="redis",
                 database_url=PG_URL, redis_url=REDIS_URL, vad_backend="energy", retention_job_enabled=False,
                 telephony_provider="twilio", twilio_account_sid="AC1", twilio_auth_token="tok",
                 telephony_public_url=PUBLIC, telephony_allow_unmapped=False)
    number = "+569" + str(uuid.uuid4().int)[:8]
    with TestClient(create_app(s, twilio=FakeTwilio())) as c:
        draft = c.post("/api/v1/agents", headers=_h(DEMO), json={
            "name": "Clínica", "instructions": "Atiende pacientes con amabilidad.", "tools": []}).json()
        assert c.post("/api/v1/phone-numbers", headers=_h(DEMO), json={"e164": number, "agent_id": draft["id"]}).status_code == 201

        r = _call(c, number)  # agente en borrador: no atiende
        assert "<Stream" not in r.text and "<Hangup/>" in r.text

        assert c.post(f"/api/v1/agents/{draft['id']}/publish", headers=_h(DEMO)).status_code == 200
        r = _call(c, number)
        assert "<Stream" in r.text

        actions = [e["action"] for e in c.get("/api/v1/audit-logs", headers=_h(DEMO)).json()["items"]]
        assert "telephony.call_received" in actions and "telephony.call_rejected" in actions
        assert "telephony.call_received" not in [e["action"] for e in c.get("/api/v1/audit-logs", headers=_h(OTHER)).json()["items"]]

        assert "<Stream" not in _call(c, "+56900000000").text  # sin asignar y modo estricto


def test_resolver_is_exact_lookup_only_and_table_stays_isolated():
    """La función no abre la tabla: sin org, omni_app sigue sin ver ningún número; con E.164 exacto, la función sí resuelve."""
    number = "+569" + str(uuid.uuid4().int)[:8]

    async def scenario():
        eng = create_async_engine(PG_URL)
        try:
            async with eng.begin() as c:
                await c.execute(text("SELECT set_config('app.org_id', :o, true)"), {"o": DEMO})
                await c.execute(text("INSERT INTO phone_numbers (org_id, e164) VALUES (:o, :e)"), {"o": DEMO, "e": number})
            try:  # sin app.org_id la política falla cerrado: error o cero filas, nunca datos
                async with eng.begin() as c:
                    direct = (await c.execute(text("SELECT count(*) FROM phone_numbers"))).scalar()
            except Exception:  # noqa: BLE001
                direct = 0
            async with eng.begin() as c:
                via_fn = (await c.execute(text("SELECT org_id::text, agent_id FROM resolve_phone_number(:e)"), {"e": number})).first()
                nothing = (await c.execute(text("SELECT count(*) FROM resolve_phone_number('+1000000000')"))).scalar()
            async with eng.begin() as c:
                await c.execute(text("SELECT set_config('app.org_id', :o, true)"), {"o": DEMO})
                await c.execute(text("DELETE FROM phone_numbers WHERE e164 = :e"), {"e": number})
            return direct, via_fn, nothing
        finally:
            await eng.dispose()

    direct, via_fn, nothing = asyncio.run(scenario())
    assert direct == 0 and via_fn == (DEMO, None) and nothing == 0
