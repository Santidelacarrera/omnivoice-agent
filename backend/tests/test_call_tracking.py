"""Etapa 3: estados de llamada, idempotencia de eventos del proveedor y API de consulta."""
import base64
import json
import uuid
from urllib.parse import urlencode

import pytest

from app.realtime.provider import FakeProvider
from app.security.auth import issue_token
from app.telephony import call_state as cs
from app.telephony.codec import ulaw_encode
from app.telephony.twilio import compute_signature
from tests.test_telephony import PUBLIC, make, signed_post, sine

ORG = str(uuid.uuid4())
OTHER = str(uuid.uuid4())
NUM = "+56911110000"
BODY = {"name": "Recepción", "instructions": "Atiende llamadas de la clínica con amabilidad.", "tools": []}


def h(org=ORG, role="admin"):
    return {"Authorization": f"Bearer {issue_token('u', org, role)}"}


def published_number(c, org=ORG, number=NUM):
    aid = c.post("/api/v1/agents", json=BODY, headers=h(org)).json()["id"]
    c.post(f"/api/v1/agents/{aid}/publish", headers=h(org))
    assert c.post("/api/v1/phone-numbers", json={"e164": number, "agent_id": aid}, headers=h(org)).status_code == 201
    return aid


def status_post(c, params, sig=None):
    return c.post("/telephony/status", content=urlencode(params), headers={
        "content-type": "application/x-www-form-urlencoded",
        "x-twilio-signature": sig or compute_signature("tok", f"{PUBLIC}/telephony/status", params)})


def only_call(c, org=ORG):
    items = c.get("/api/v1/calls", headers=h(org)).json()["items"]
    assert len(items) == 1, items
    return c.get(f"/api/v1/calls/{items[0]['id']}", headers=h(org)).json()


def run_ws_call(c, sid="CA1"):
    twiml = signed_post(c, {"CallSid": sid, "From": "+34600000000", "To": NUM}).text
    ticket = twiml.split('name="ticket" value="')[1].split('"')[0]
    frame = base64.b64encode(ulaw_encode(sine(8000, 20))).decode()
    start = {"event": "start", "start": {"streamSid": "MZ1", "callSid": sid, "customParameters": {"ticket": ticket, "call_sid": sid}}}
    with c.websocket_connect("/ws/telephony") as ws:
        ws.send_text(json.dumps(start))
        for _ in range(3):
            ws.send_text(json.dumps({"event": "media", "streamSid": "MZ1", "media": {"payload": frame}}))
        ws.receive_text()
        ws.send_text(json.dumps({"event": "stop"}))


# ---------- máquina de estados ----------
@pytest.mark.parametrize("cur,new,ok", [
    (cs.CREATED, cs.CONNECTED, True), (cs.CREATED, cs.RINGING, True), (cs.QUEUED, cs.RINGING, True),
    (cs.CONNECTED, cs.ANSWERED, False), (cs.CONNECTED, cs.RINGING, False), (cs.RINGING, cs.RINGING, False),
    (cs.RINGING, cs.NO_ANSWER, True), (cs.QUEUED, cs.BUSY, True), (cs.CREATED, cs.FAILED, True),
    (cs.ANSWERED, cs.TRANSFERRED, True), (cs.CONNECTED, cs.TRANSFERRED, True), (cs.RINGING, cs.TRANSFERRED, False),
    (cs.COMPLETED, cs.CONNECTED, False), (cs.TRANSFERRED, cs.COMPLETED, False), (cs.FAILED, cs.COMPLETED, False),
    ("NOPE", cs.COMPLETED, False), (cs.CREATED, "NOPE", False),
])
def test_transitions(cur, new, ok):
    assert cs.can_transition(cur, new) is ok


def test_twilio_status_mapping():
    assert cs.from_twilio("in-progress") == cs.ANSWERED and cs.from_twilio("no-answer") == cs.NO_ANSWER
    assert cs.from_twilio("canceled") == cs.CANCELLED and cs.from_twilio("raro") is None and cs.from_twilio("") is None
    assert {cs.from_twilio(x) for x in ("completed", "busy", "failed")} <= cs.TERMINAL


# ---------- llamada entrante completa ----------
def test_inbound_call_lifecycle_is_recorded():
    c, _ = make(FakeProvider(respond_on_audio=True, script=[{"type": "transcript_agent", "text": "hola"}]))
    published_number(c)
    run_ws_call(c)
    call = only_call(c)
    assert call["status"] == cs.COMPLETED and call["direction"] == "inbound" and call["answered_at"] and call["ended_at"]
    assert [e["status"] for e in call["events"]] == [cs.CREATED, cs.CONNECTED, cs.COMPLETED]
    assert "34600000000" not in json.dumps(call)  # solo hash del interlocutor


def test_provider_status_callbacks_never_regress_and_are_idempotent():
    c, _ = make(FakeProvider(respond_on_audio=True, script=[{"type": "transcript_agent", "text": "hola"}]))
    published_number(c)
    run_ws_call(c)
    before = len(only_call(c)["events"])
    for status, seq in (("in-progress", "2"), ("completed", "3"), ("completed", "3")):  # tardío, final y repetido
        assert status_post(c, {"CallSid": "CA1", "CallStatus": status, "SequenceNumber": seq}).status_code == 204
    call = only_call(c)
    assert call["status"] == cs.COMPLETED
    assert len(call["events"]) == before + 2  # el repetido no añade evento
    late = [e for e in call["events"] if e["event_key"] == "tw:in-progress:2"][0]
    assert late["applied"] is False  # llegó después de CONNECTED: se guarda pero no retrocede


def test_status_callback_validation():
    c, _ = make()
    assert status_post(c, {"CallSid": "CA1", "CallStatus": "completed"}, sig="falsa").status_code == 403
    assert status_post(c, {"CallSid": "CAdesconocida", "CallStatus": "completed"}).status_code == 204
    published_number(c)
    signed_post(c, {"CallSid": "CA9", "From": "+34600000000", "To": NUM})
    assert status_post(c, {"CallSid": "CA9", "CallStatus": "raro"}).status_code == 204
    assert only_call(c)["status"] == cs.CREATED


def test_duplicate_webhook_after_connected_is_refused_but_retry_before_is_not():
    c, _ = make(FakeProvider(respond_on_audio=True, script=[{"type": "transcript_agent", "text": "hola"}]))
    published_number(c)
    p = {"CallSid": "CA1", "From": "+34600000000", "To": NUM}
    assert "<Stream" in signed_post(c, p).text
    assert "<Stream" in signed_post(c, p).text  # reintento antes de conectar: se atiende
    assert len(c.get("/api/v1/calls", headers=h()).json()["items"]) == 1  # sigue siendo una sola llamada
    run_ws_call(c)
    r = signed_post(c, p)  # ya terminó
    assert "<Stream" not in r.text and "ya fue atendida" in r.text
    assert "telephony.call_duplicate" in [e["action"] for e in c.get("/api/v1/audit-logs", headers=h()).json()["items"]]


def test_rejected_unpublished_agent_is_recorded_as_failed():
    c, _ = make()
    aid = c.post("/api/v1/agents", json=BODY, headers=h()).json()["id"]
    c.post("/api/v1/phone-numbers", json={"e164": NUM, "agent_id": aid}, headers=h())
    signed_post(c, {"CallSid": "CA5", "From": "+34600000000", "To": NUM})
    call = only_call(c)
    assert call["status"] == cs.FAILED and call["events"][-1]["detail"]["reason"] == "agent_not_published"


# ---------- llamadas salientes ----------
def test_outbound_call_tracks_ringing_to_no_answer():
    c, tw = make()
    published_number(c)
    r = c.post("/api/v1/telephony/calls", json={"to": "+34622222222", "consent_basis": "existing_customer"}, headers=h())
    assert r.status_code == 202
    assert tw.calls[0][2].endswith("/telephony/voice?direction=outbound")
    call = only_call(c)
    assert call["direction"] == "outbound" and call["status"] == cs.QUEUED
    for status, seq in (("ringing", "1"), ("no-answer", "2")):
        status_post(c, {"CallSid": "CAout", "CallStatus": status, "SequenceNumber": seq})
    assert only_call(c)["status"] == cs.NO_ANSWER


# ---------- consulta ----------
def test_calls_api_permissions_and_isolation():
    c, _ = make()
    published_number(c)
    c.post("/api/v1/telephony/calls", json={"to": "+34622222222", "consent_basis": "existing_customer"}, headers=h())
    cid = only_call(c)["id"]
    for role, code in (("viewer", 200), ("analyst", 200), ("operator", 200), ("customer", 403)):
        assert c.get("/api/v1/calls", headers=h(ORG, role)).status_code == code
    assert c.get("/api/v1/calls", headers=h(OTHER)).json()["items"] == []
    assert c.get(f"/api/v1/calls/{cid}", headers=h(OTHER)).status_code == 404
    assert c.get("/api/v1/calls/no-uuid", headers=h()).status_code == 404
    assert c.get("/api/v1/calls", headers={}).status_code == 401
