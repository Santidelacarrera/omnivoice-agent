"""Telefonía: códec μ-law, firma de Twilio, webhook, Media Streams y transferencia a humano."""
import array
import asyncio
import base64
import json
import math

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app
from app.realtime.provider import FakeProvider
from app.security.auth import issue_token
from app.telephony.codec import platform_to_telephony, telephony_to_platform, ulaw_decode, ulaw_encode
from app.telephony.twilio import compute_signature, dial_twiml, stream_twiml, validate_signature
from app.tools.handlers import DEMO_ORG

PUBLIC = "https://voz.example.com"


def sine(rate, ms=100, freq=440, amp=12000):
    n = rate * ms // 1000
    return array.array("h", (int(amp * math.sin(2 * math.pi * freq * i / rate)) for i in range(n))).tobytes()


def test_ulaw_roundtrip_error_is_small():
    pcm = sine(8000)
    back = array.array("h"); back.frombytes(ulaw_decode(ulaw_encode(pcm)))
    orig = array.array("h"); orig.frombytes(pcm)
    # G.711 introduce error relativo de ~3 %: se acepta hasta 5 % del rango de la señal.
    assert max(abs(a - b) for a, b in zip(orig, back)) < 0.05 * 12000 + 300


def test_ulaw_known_values():
    assert ulaw_encode(b"\x00\x00") == b"\xff"          # silencio positivo
    assert ulaw_decode(b"\xff") == b"\x00\x00"
    assert len(ulaw_encode(sine(8000, 20))) == 160       # 20 ms a 8 kHz


def test_sample_rate_conversion_lengths():
    ulaw = ulaw_encode(sine(8000, 20))
    pcm24 = telephony_to_platform(ulaw, 24000)
    assert len(pcm24) // 2 == 480                        # 20 ms a 24 kHz
    assert len(platform_to_telephony(pcm24, 24000)) == 160


def test_signature_validation():
    params = {"CallSid": "CA1", "From": "+34600000000"}
    sig = compute_signature("tok", f"{PUBLIC}/telephony/voice", params)
    assert validate_signature("tok", f"{PUBLIC}/telephony/voice", params, sig)
    assert not validate_signature("tok", f"{PUBLIC}/telephony/voice", {**params, "From": "+1"}, sig)
    assert not validate_signature("otro", f"{PUBLIC}/telephony/voice", params, sig)
    assert not validate_signature("tok", f"{PUBLIC}/telephony/voice", params, "")


def test_twiml_is_escaped():
    x = stream_twiml("wss://h/ws", {"a": 'x"><evil/>'})
    assert "<evil/>" not in x and "&lt;evil/&gt;" in x
    assert "<Dial>+34911111111</Dial>" in dial_twiml("+34911111111")


class FakeTwilio:
    def __init__(self): self.calls, self.redirects = [], []
    async def create_call(self, to, from_, url, status_callback=None):
        self.calls.append((to, from_, url)); return {"sid": "CAout", "status": "queued"}
    async def redirect_call(self, sid, twiml): self.redirects.append((sid, twiml))


def make(provider=None, **over):
    s = Settings(environment="development", persistence_backend="memory", state_backend="memory", openai_api_key="",
                 telephony_provider="twilio", twilio_account_sid="AC1", twilio_auth_token="tok", twilio_from_number="+34910000000",
                 telephony_public_url=PUBLIC, human_transfer_number="+34911111111", transfer_announce_ms=10,
                 vad_backend="energy", retention_job_enabled=False, **over)
    tw = FakeTwilio()
    app = create_app(s, provider_factory=(lambda: provider) if provider else (lambda: FakeProvider()), twilio=tw)
    return TestClient(app), tw


def signed_post(c, params):
    return c.post("/telephony/voice", content="&".join(f"{k}={v}" for k, v in params.items()),
                  headers={"content-type": "application/x-www-form-urlencoded",
                           "x-twilio-signature": compute_signature("tok", f"{PUBLIC}/telephony/voice", params)})


def test_webhook_rejects_bad_signature():
    c, _ = make()
    r = c.post("/telephony/voice", content="CallSid=CA1&From=%2B34600", headers={
        "content-type": "application/x-www-form-urlencoded", "x-twilio-signature": "falsa"})
    assert r.status_code == 403


def test_webhook_returns_stream_twiml_without_phone_number_in_it():
    c, _ = make()
    r = signed_post(c, {"CallSid": "CA1", "From": "+34600000000"})
    assert r.status_code == 200 and "<Stream" in r.text and "wss://voz.example.com/ws/telephony" in r.text
    assert "34600000000" not in r.text


def test_full_call_flow_audio_both_ways_and_ticket_single_use():
    c, _ = make(FakeProvider(respond_on_audio=True, script=[{"type": "transcript_agent", "text": "hola"}]))
    twiml = signed_post(c, {"CallSid": "CA1", "From": "+34600000000"}).text
    ticket = twiml.split('name="ticket" value="')[1].split('"')[0]
    frame = base64.b64encode(ulaw_encode(sine(8000, 20))).decode()
    start = {"event": "start", "start": {"streamSid": "MZ1", "callSid": "CA1", "customParameters": {"ticket": ticket, "call_sid": "CA1"}}}
    with c.websocket_connect("/ws/telephony") as ws:
        ws.send_text(json.dumps(start))
        for _ in range(3):
            ws.send_text(json.dumps({"event": "media", "streamSid": "MZ1", "media": {"payload": frame}}))
        out = json.loads(ws.receive_text())
        assert out["event"] == "media" and out["streamSid"] == "MZ1"
        assert len(base64.b64decode(out["media"]["payload"])) > 0
        ws.send_text(json.dumps({"event": "stop"}))
    with c.websocket_connect("/ws/telephony") as ws:  # el ticket ya se consumió
        ws.send_text(json.dumps(start))
        with pytest.raises(Exception):
            ws.receive_text()


def test_outbound_requires_role_and_e164():
    c, tw = make()
    body = {"to": "+34622222222"}
    cust = {"Authorization": "Bearer " + issue_token("u", DEMO_ORG, "customer")}
    adm = {"Authorization": "Bearer " + issue_token("a", DEMO_ORG, "admin")}
    assert c.post("/api/v1/telephony/calls", json=body, headers=cust).status_code == 403
    assert c.post("/api/v1/telephony/calls", json={"to": "612345"}, headers=adm).status_code == 422
    r = c.post("/api/v1/telephony/calls", json=body, headers=adm)
    assert r.status_code == 202 and r.json()["call_sid"] == "CAout"
    assert tw.calls[0][2] == f"{PUBLIC}/telephony/voice?direction=outbound"


def test_transfer_to_human_redirects_the_call():
    prov = FakeProvider(script=[{"type": "tool_call", "name": "transfer_to_human", "call_id": "c1",
                                 "arguments": json.dumps({"reason": "cliente pide humano"})}], respond_on_audio=True)
    c, tw = make(prov)
    ticket = signed_post(c, {"CallSid": "CA9", "From": "+34600000001"}).text.split('name="ticket" value="')[1].split('"')[0]
    frame = base64.b64encode(ulaw_encode(sine(8000, 20))).decode()
    with c.websocket_connect("/ws/telephony") as ws:
        ws.send_text(json.dumps({"event": "start", "start": {"streamSid": "MZ2", "callSid": "CA9", "customParameters": {"ticket": ticket}}}))
        ws.send_text(json.dumps({"event": "media", "streamSid": "MZ2", "media": {"payload": frame}}))
        import time
        for _ in range(100):
            if tw.redirects:
                break
            time.sleep(0.02)
        ws.send_text(json.dumps({"event": "stop"}))
    assert tw.redirects and tw.redirects[0][0] == "CA9"
    assert "<Dial" in tw.redirects[0][1] and "+34911111111" in tw.redirects[0][1]
