"""Etapa 3 contra PostgreSQL real: llamadas, eventos idempotentes, RLS y función de resolución (migración 007)."""
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
    def __init__(self): self.calls, self.redirects = [], []
    async def create_call(self, to, from_, url, status_callback=None):
        self.calls.append((to, from_, url, status_callback)); return {"sid": "CA" + uuid.uuid4().hex, "status": "queued"}
    async def redirect_call(self, sid, twiml): self.redirects.append((sid, twiml))
    async def hangup(self, sid, cancel=False): return None


def _h(org, role="admin"):
    return {"Authorization": f"Bearer {issue_token('u-calls', org, role)}"}


def _status(c, sid, status, seq):
    params = {"CallSid": sid, "CallStatus": status, "SequenceNumber": seq}
    return c.post("/telephony/status", content=urlencode(params), headers={
        "content-type": "application/x-www-form-urlencoded",
        "x-twilio-signature": compute_signature("tok", f"{PUBLIC}/telephony/status", params)})


def _settings():
    return Settings(environment="test", jwt_secret=get_settings().jwt_secret, persistence_backend="postgres", state_backend="redis",
                    database_url=PG_URL, redis_url=REDIS_URL, vad_backend="energy", retention_job_enabled=False,
                    telephony_provider="twilio", twilio_account_sid="AC1", twilio_auth_token="tok", twilio_from_number="+34910000000",
                    telephony_public_url=PUBLIC)


def test_outbound_call_status_flow_on_postgres():
    tw = FakeTwilio()
    with TestClient(create_app(_settings(), twilio=tw)) as c:
        assert c.post("/api/v1/telephony/calls", headers=_h(DEMO), json={"to": "+34622222222"}).status_code == 202
        sid = tw.calls[0] and None
        sid = [x for x in c.get("/api/v1/calls", headers=_h(DEMO)).json()["items"] if x["status"] == "QUEUED"][0]["sid"]
        assert tw.calls[0][3] == f"{PUBLIC}/telephony/status"

        assert _status(c, sid, "ringing", "1").status_code == 204
        assert _status(c, sid, "in-progress", "2").status_code == 204
        assert _status(c, sid, "in-progress", "2").status_code == 204        # repetido: sin efecto
        assert _status(c, sid, "completed", "3").status_code == 204
        assert _status(c, sid, "ringing", "9").status_code == 204             # tardío: no retrocede

        items = c.get("/api/v1/calls", headers=_h(DEMO)).json()["items"]
        call = c.get(f"/api/v1/calls/{[i for i in items if i['sid'] == sid][0]['id']}", headers=_h(DEMO)).json()
        assert call["status"] == "COMPLETED" and call["answered_at"] and call["ended_at"]
        assert [(e["status"], e["applied"]) for e in call["events"]] == [
            ("CREATED", True), ("QUEUED", True), ("RINGING", True), ("ANSWERED", True), ("COMPLETED", True), ("RINGING", False)]

        # otra organización no ve la llamada
        assert c.get(f"/api/v1/calls/{call['id']}", headers=_h(OTHER)).status_code == 404
        assert all(i["sid"] != sid for i in c.get("/api/v1/calls", headers=_h(OTHER)).json()["items"])


def test_call_tables_are_isolated_and_append_only():
    async def scenario():
        eng = create_async_engine(PG_URL)
        try:
            sid = "CA" + uuid.uuid4().hex
            async with eng.begin() as c:
                await c.execute(text("SELECT set_config('app.org_id', :o, true)"), {"o": DEMO})
                cid = (await c.execute(text("INSERT INTO calls (org_id, provider, provider_call_sid, direction) "
                                            "VALUES (:o, 'twilio', :s, 'inbound') RETURNING id"), {"o": DEMO, "s": sid})).scalar()
                await c.execute(text("INSERT INTO call_events (call_id, org_id, status, event_key, applied) "
                                     "VALUES (:c, :o, 'CREATED', 'created', true)"), {"c": cid, "o": DEMO})
            async with eng.begin() as c:  # función de resolución: devuelve la organización con el id exacto, nada más
                org = (await c.execute(text("SELECT resolve_call_org('twilio', :s)"), {"s": sid})).scalar()
                none = (await c.execute(text("SELECT resolve_call_org('twilio', 'CAinexistente')"))).scalar()
            async with eng.begin() as c:  # otra organización: no ve filas
                await c.execute(text("SELECT set_config('app.org_id', :o, true)"), {"o": OTHER})
                seen = (await c.execute(text("SELECT count(*) FROM calls WHERE provider_call_sid = :s"), {"s": sid})).scalar()
            denied = False
            try:  # el historial de eventos no se borra ni se edita
                async with eng.begin() as c:
                    await c.execute(text("SELECT set_config('app.org_id', :o, true)"), {"o": DEMO})
                    await c.execute(text("DELETE FROM call_events WHERE call_id = :c"), {"c": cid})
            except Exception:  # noqa: BLE001
                denied = True
            return str(org), none, seen, denied
        finally:
            await eng.dispose()

    org, none, seen, denied = asyncio.run(scenario())
    assert org == DEMO and none is None and seen == 0 and denied


def test_transfer_with_context_on_postgres_and_redis():
    """Traspaso real: TRANSFERRED en PostgreSQL, contexto de un solo uso en Redis (GETDEL) y sin texto de conversación en la base."""
    import time
    from urllib.parse import parse_qs, urlparse

    tw = FakeTwilio()
    s = _settings().model_copy(update={"human_transfer_number": "+34911111111", "transfer_announce_ms": 10})
    with TestClient(create_app(s, twilio=tw)) as c:
        assert c.post("/api/v1/telephony/calls", headers=_h(DEMO), json={"to": "+34633333333"}).status_code == 202
        items = c.get("/api/v1/calls", headers=_h(DEMO)).json()["items"]
        call = [i for i in items if i["status"] == "QUEUED"][0]
        assert c.post(f"/api/v1/calls/{call['id']}/transfer", headers=_h(DEMO), json={}).status_code == 409  # aún sin contestar
        _status(c, call["sid"], "in-progress", "2")
        assert c.post(f"/api/v1/calls/{call['id']}/transfer", headers=_h(DEMO, "viewer"), json={}).status_code == 403
        assert c.post(f"/api/v1/calls/{call['id']}/transfer", headers=_h(DEMO), json={"reason": "supervisor"}).status_code == 202
        for _ in range(100):
            if getattr(tw, "redirects", None):
                break
            time.sleep(0.02)
        twiml = tw.redirects[0][1]
        token = parse_qs(urlparse(twiml.split('<Number url="')[1].split('"')[0].replace("&amp;", "&")).query)["h"][0]

        params = {}
        sig = compute_signature("tok", f"{PUBLIC}/telephony/whisper?h={token}", params)
        post = lambda: c.post(f"/telephony/whisper?h={token}", content="", headers={  # noqa: E731
            "content-type": "application/x-www-form-urlencoded", "x-twilio-signature": sig})
        assert "supervisor" in post().text and "<Say" not in post().text

        detail = c.get(f"/api/v1/calls/{call['id']}", headers=_h(DEMO)).json()
        assert detail["status"] == "TRANSFERRED"
        assert [e["detail"] for e in detail["events"] if e["status"] == "TRANSFERRED"] == [{"source": "api", "context": True}]
        assert "supervisor" not in str(detail)
        assert c.post(f"/api/v1/calls/{call['id']}/end", headers=_h(DEMO)).status_code == 409
        assert c.post(f"/api/v1/calls/{call['id']}/transfer", headers=_h(OTHER), json={}).status_code == 404
