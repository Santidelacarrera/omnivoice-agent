"""Etapa 3: el número marcado decide organización y agente (multi-tenant), y solo atiende un agente publicado."""
import base64
import json
import uuid

import pytest

from app.realtime.provider import FakeProvider
from app.security.auth import issue_token
from app.telephony.codec import ulaw_encode
from tests.test_telephony import make, signed_post, sine

ORG_A, ORG_B = str(uuid.uuid4()), str(uuid.uuid4())
NUM_A, NUM_B = "+56911110000", "+56922220000"
BODY = {"name": "Recepción", "instructions": "Atiende llamadas de la clínica con amabilidad.", "tools": []}


def h(org, role="admin"):
    return {"Authorization": f"Bearer {issue_token('u', org, role)}"}


def setup_number(c, org, number, publish=True, with_agent=True):
    agent_id = None
    if with_agent:
        agent_id = c.post("/api/v1/agents", json=BODY, headers=h(org)).json()["id"]
        if publish:
            assert c.post(f"/api/v1/agents/{agent_id}/publish", headers=h(org)).status_code == 200
    r = c.post("/api/v1/phone-numbers", json={"e164": number, "agent_id": agent_id}, headers=h(org))
    assert r.status_code == 201, r.text
    return agent_id


def actions(c, org):
    return [e["action"] for e in c.get("/api/v1/audit-logs", headers=h(org)).json()["items"]]


def call(c, to, **extra):
    return signed_post(c, {"CallSid": "CA1", "From": "+34600000000", "To": to, **extra})


def test_published_agent_number_streams_and_is_audited_in_its_own_org():
    c, _ = make()
    setup_number(c, ORG_A, NUM_A)
    r = call(c, NUM_A)
    assert "<Stream" in r.text and "34600000000" not in r.text
    assert "telephony.call_received" in actions(c, ORG_A)
    assert "telephony.call_received" not in actions(c, ORG_B)


@pytest.mark.parametrize("publish,with_agent,reason", [(False, True, "agent_not_published"), (True, False, "no_agent")])
def test_unpublished_or_missing_agent_is_rejected(publish, with_agent, reason):
    c, _ = make()
    setup_number(c, ORG_A, NUM_A, publish=publish, with_agent=with_agent)
    r = call(c, NUM_A)
    assert "<Stream" not in r.text and "<Hangup/>" in r.text
    audit = c.get("/api/v1/audit-logs", headers=h(ORG_A)).json()["items"]
    assert any(e["action"] == "telephony.call_rejected" and e["detail"]["reason"] == reason for e in audit)


def test_archived_agent_stops_answering():
    c, _ = make()
    aid = setup_number(c, ORG_A, NUM_A)
    assert "<Stream" in call(c, NUM_A).text
    c.post(f"/api/v1/agents/{aid}/archive", headers=h(ORG_A))
    assert "<Stream" not in call(c, NUM_A).text


def test_two_orgs_route_independently():
    c, _ = make()
    setup_number(c, ORG_A, NUM_A)
    setup_number(c, ORG_B, NUM_B)
    call(c, NUM_A)
    call(c, NUM_B)
    assert actions(c, ORG_A).count("telephony.call_received") == 1
    assert actions(c, ORG_B).count("telephony.call_received") == 1


def test_unmapped_number_falls_back_by_default_but_can_be_rejected():
    c, _ = make()
    assert "<Stream" in call(c, "+56999999999").text  # compatibilidad con configuración global
    strict, _ = make(telephony_allow_unmapped=False)
    r = call(strict, "+56999999999")
    assert "<Stream" not in r.text and "no está disponible" in r.text


def test_outbound_call_resolves_our_number_from_the_from_field():
    c, _ = make()
    setup_number(c, ORG_A, NUM_A)
    r = signed_post(c, {"CallSid": "CA2", "From": NUM_A, "To": "+34600000000", "Direction": "outbound-api"})
    assert "<Stream" in r.text and "telephony.call_received" in actions(c, ORG_A)


def test_mapped_call_runs_end_to_end_with_its_agent():
    c, _ = make(FakeProvider(respond_on_audio=True, script=[{"type": "transcript_agent", "text": "hola"}]))
    setup_number(c, ORG_A, NUM_A)
    ticket = call(c, NUM_A).text.split('name="ticket" value="')[1].split('"')[0]
    frame = base64.b64encode(ulaw_encode(sine(8000, 20))).decode()
    start = {"event": "start", "start": {"streamSid": "MZ1", "callSid": "CA1", "customParameters": {"ticket": ticket, "call_sid": "CA1"}}}
    with c.websocket_connect("/ws/telephony") as ws:
        ws.send_text(json.dumps(start))
        for _ in range(3):
            ws.send_text(json.dumps({"event": "media", "streamSid": "MZ1", "media": {"payload": frame}}))
        assert json.loads(ws.receive_text())["event"] == "media"
        ws.send_text(json.dumps({"event": "stop"}))
