"""Etapa 3: transferencia a humano con contexto (whisper), API de transferir/finalizar y privacidad del contexto."""
import base64
import json
import time
import uuid
from urllib.parse import parse_qs, urlparse

from app.realtime.provider import FakeProvider
from app.security.auth import issue_token
from app.telephony import call_state as cs
from app.telephony.codec import ulaw_encode
from app.telephony.handoff import MAX_BRIEFING_CHARS, build_briefing
from app.telephony.twilio import compute_signature, dial_twiml
from tests.test_telephony import PUBLIC, make, signed_post, sine

ORG = str(uuid.uuid4())
NUM = "+56911110000"
BODY = {"name": "Recepción", "instructions": "Atiende llamadas de la clínica con amabilidad.", "tools": []}
SECRET_TEXT = "mi rut es 12.345.678-9"


def h(org=ORG, role="admin"):
    return {"Authorization": f"Bearer {issue_token('u', org, role)}"}


def setup(c):
    aid = c.post("/api/v1/agents", json=BODY, headers=h()).json()["id"]
    c.post(f"/api/v1/agents/{aid}/publish", headers=h())
    c.post("/api/v1/phone-numbers", json={"e164": NUM, "agent_id": aid}, headers=h())


def whisper_token(redirect_twiml: str) -> str:
    url = redirect_twiml.split("<Number url=\"")[1].split("\"")[0].replace("&amp;", "&")
    return parse_qs(urlparse(url).query)["h"][0]


def post_whisper(c, token, sig=None):
    url = f"{PUBLIC}/telephony/whisper?h={token}"
    return c.post(f"/telephony/whisper?h={token}", content="", headers={
        "content-type": "application/x-www-form-urlencoded", "x-twilio-signature": sig or compute_signature("tok", url, {})})


def open_call(c, sid="CA9"):
    """Conecta una llamada por WebSocket y la deja abierta; devuelve (ws_cm, ws)."""
    ticket = signed_post(c, {"CallSid": sid, "From": "+34600000001", "To": NUM}).text.split('name="ticket" value="')[1].split('"')[0]
    cm = c.websocket_connect("/ws/telephony")
    ws = cm.__enter__()
    ws.send_text(json.dumps({"event": "start", "start": {"streamSid": "MZ2", "callSid": sid, "customParameters": {"ticket": ticket}}}))
    return cm, ws


def wait(pred, tries=150):
    for _ in range(tries):
        if pred():
            return True
        time.sleep(0.02)
    return False


# ---------- briefing ----------
def test_briefing_has_reason_and_latest_turns_and_is_bounded():
    turns = [("user", f"mensaje {i} " + "x" * 120) for i in range(6)] + [("agent", "Le paso con un operador"), ("user", "gracias")]
    b = build_briefing("cliente pide humano", turns)
    assert "Motivo: cliente pide humano" in b and "gracias" in b and len(b) <= MAX_BRIEFING_CHARS
    assert "mensaje 0" not in b  # lo antiguo se recorta, no lo reciente


def test_briefing_without_context_and_from_panel():
    assert build_briefing("", []) == "Transferencia de la llamada."
    assert "panel" in build_briefing("", [], "api")


def test_dial_twiml_whisper_is_escaped_and_optional():
    assert "<Number" not in dial_twiml("+34911111111")
    x = dial_twiml("+34911111111", "+34910000000", 'https://h/w?h=1&x="><evil/>')
    assert "<evil/>" not in x and "<Number url=" in x and ">+34911111111</Number>" in x


# ---------- el agente transfiere: el operador recibe contexto una sola vez ----------
def test_agent_transfer_whispers_context_to_operator_once_and_does_not_store_it():
    prov = FakeProvider(script=[{"type": "transcript_user", "text": SECRET_TEXT},
                                {"type": "tool_call", "name": "transfer_to_human", "call_id": "c1",
                                 "arguments": json.dumps({"reason": "cliente pide humano"})}], respond_on_audio=True)
    c, tw = make(prov)
    setup(c)
    cm, ws = open_call(c)
    frame = base64.b64encode(ulaw_encode(sine(8000, 20))).decode()
    ws.send_text(json.dumps({"event": "media", "streamSid": "MZ2", "media": {"payload": frame}}))
    assert wait(lambda: tw.redirects)
    twiml = tw.redirects[0][1]
    assert "+34911111111" in twiml and "<Number url=" in twiml
    token = whisper_token(twiml)

    r = post_whisper(c, token)
    assert "cliente pide humano" in r.text and SECRET_TEXT in r.text and "<Say" in r.text
    again = post_whisper(c, token)  # de un solo uso
    assert "<Say" not in again.text and "<Response>" in again.text
    assert post_whisper(c, "inventado").text.count("<Say") == 0
    ws.send_text(json.dumps({"event": "stop"}))
    cm.__exit__(None, None, None)

    calls = c.get("/api/v1/calls", headers=h()).json()["items"]
    detail = c.get(f"/api/v1/calls/{calls[0]['id']}", headers=h()).json()
    assert detail["status"] == cs.TRANSFERRED  # el completed posterior no lo reescribe
    assert SECRET_TEXT not in json.dumps(detail) and "cliente pide humano" not in json.dumps(detail)
    ev = [e for e in detail["events"] if e["status"] == cs.TRANSFERRED][0]
    assert ev["detail"] == {"source": "agent", "context": True}


def test_whisper_rejects_bad_signature():
    c, _ = make()
    assert post_whisper(c, "x", sig="falsa").status_code == 403


# ---------- API ----------
def connected_call(c, **kw):
    cm, ws = open_call(c, **kw)
    assert wait(lambda: c.get("/api/v1/calls", headers=h()).json()["items"][0]["status"] == cs.CONNECTED)
    return cm, ws, c.get("/api/v1/calls", headers=h()).json()["items"][0]["id"]


def test_api_transfer_uses_live_context_and_is_not_repeatable():
    c, tw = make(FakeProvider(respond_on_audio=True, script=[{"type": "transcript_user", "text": "necesito hablar con alguien"}]))
    setup(c)
    cm, ws, cid = connected_call(c)
    frame = base64.b64encode(ulaw_encode(sine(8000, 20))).decode()
    ws.send_text(json.dumps({"event": "media", "streamSid": "MZ2", "media": {"payload": frame}}))
    time.sleep(0.2)
    assert c.post(f"/api/v1/calls/{cid}/transfer", json={"reason": "supervisor"}, headers=h(ORG, "viewer")).status_code == 403
    r = c.post(f"/api/v1/calls/{cid}/transfer", json={"reason": "supervisor"}, headers=h())
    assert r.status_code == 202 and wait(lambda: tw.redirects)
    page = post_whisper(c, whisper_token(tw.redirects[0][1])).text
    assert "supervisor" in page and "panel" in page
    assert c.post(f"/api/v1/calls/{cid}/transfer", json={}, headers=h()).status_code == 409  # ya transferida
    ws.send_text(json.dumps({"event": "stop"}))
    cm.__exit__(None, None, None)


def test_api_transfer_unavailable_without_operator_number_and_unknown_calls():
    c, tw = make(human_transfer_number="")
    setup(c)
    cm, ws, cid = connected_call(c)
    assert c.post(f"/api/v1/calls/{cid}/transfer", json={}, headers=h()).status_code == 409 and not tw.redirects
    assert c.post(f"/api/v1/calls/{uuid.uuid4()}/transfer", json={}, headers=h()).status_code == 404
    assert c.post("/api/v1/calls/no-uuid/transfer", json={}, headers=h()).status_code == 404
    assert c.post(f"/api/v1/calls/{cid}/transfer", json={}, headers=h(str(uuid.uuid4()))).status_code == 404  # otra organización
    ws.send_text(json.dumps({"event": "stop"}))
    cm.__exit__(None, None, None)


def test_api_cannot_transfer_before_answer_but_can_cancel_outbound():
    c, tw = make()
    assert c.post("/api/v1/telephony/calls", json={"to": "+34622222222"}, headers=h()).status_code == 202
    cid = c.get("/api/v1/calls", headers=h()).json()["items"][0]["id"]
    assert c.post(f"/api/v1/calls/{cid}/transfer", json={}, headers=h()).status_code == 409
    assert c.post(f"/api/v1/calls/{cid}/end", headers=h(ORG, "operator")).status_code == 202
    assert tw.hangups == [("CAout", True)]
    assert c.get(f"/api/v1/calls/{cid}", headers=h()).json()["status"] == cs.CANCELLED
    assert c.post(f"/api/v1/calls/{cid}/end", headers=h()).status_code == 409  # ya terminó
    audit = [e["action"] for e in c.get("/api/v1/audit-logs", headers=h()).json()["items"]]
    assert "call.end_requested" in audit


def test_api_end_connected_call_completes_it():
    c, tw = make(FakeProvider(respond_on_audio=True, script=[{"type": "transcript_agent", "text": "hola"}]))
    setup(c)
    cm, ws, cid = connected_call(c)
    assert c.post(f"/api/v1/calls/{cid}/end", headers=h()).status_code == 202
    assert tw.hangups == [("CA9", False)]
    assert c.get(f"/api/v1/calls/{cid}", headers=h()).json()["status"] == cs.COMPLETED
    ws.send_text(json.dumps({"event": "stop"}))
    cm.__exit__(None, None, None)
