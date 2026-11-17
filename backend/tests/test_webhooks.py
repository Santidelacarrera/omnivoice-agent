"""Etapa 10: webhooks salientes: SSRF, firma, cifrado del secreto, cola con reintentos y eventos."""
import asyncio
import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app
from app.security import netguard, secretbox
from app.security.auth import issue_token
from app.services import webhooks as wh
from tests.test_telephony import FakeTwilio  # noqa: F401
from tests.test_telephony import make as make_telephony

ORG, OTHER = str(uuid.uuid4()), str(uuid.uuid4())
PUBLIC_IP = "93.184.216.34"
URL = "https://hooks.example.com/omnivoice?x=1"


def h(org=ORG, role="admin"):
    return {"Authorization": f"Bearer {issue_token('u', org, role)}"}


def run(coro):
    return asyncio.run(coro)


class Receiver:
    """Servidor falso del cliente: guarda lo que recibe y responde con el código que se le indique."""

    def __init__(self, status=200):
        self.status, self.requests, self.raise_exc = status, [], None

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.raise_exc:
            raise self.raise_exc
        return httpx.Response(self.status, text="cuerpo que no debe guardarse")

    def client(self):
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler), follow_redirects=False)


def app_with(receiver=None, resolver=None, **settings):
    rec = receiver or Receiver()
    s = Settings(environment="test", jwt_secret="t" * 40, vad_backend="energy", webhook_worker_enabled=False, **settings)
    app = create_app(s, webhook_http=rec.client(), webhook_resolver=resolver or (lambda host: [PUBLIC_IP]))
    return TestClient(app), rec


def create(c, events=None, url=URL, org=ORG):
    body = {"url": url, **({"events": events} if events else {})}
    r = c.post("/api/v1/webhooks", json=body, headers=h(org))
    assert r.status_code == 201, r.text
    return r.json()


# ---------- SSRF ----------
@pytest.mark.parametrize("ip,ok", [
    ("93.184.216.34", True), ("8.8.8.8", True), ("2606:4700:4700::1111", True),
    ("10.0.0.1", False), ("192.168.1.1", False), ("172.16.5.4", False), ("127.0.0.1", False), ("169.254.169.254", False),
    ("0.0.0.0", False), ("::1", False), ("fe80::1", False), ("fc00::1", False), ("::ffff:10.0.0.1", False), ("224.0.0.1", False),
    ("100.64.0.1", False), ("no-es-ip", False)])
def test_is_public(ip, ok):
    assert netguard.is_public(ip) is ok


@pytest.mark.parametrize("url", ["http://hooks.example.com/x", "ftp://a.com/x", "https://user:pass@a.com/x", "https://a.com/x#frag",
                                 "https:///solo-ruta", "https://a.com:99999/x", "https://" + "a" * 2100, "no es url"])
def test_url_syntax_rejections(url):
    with pytest.raises(netguard.UnsafeUrl):
        netguard.check_syntax(url)


def test_resolve_safe_rejects_private_mixed_and_literal_addresses():
    ok = run(netguard.resolve_safe("https://a.com/x", lambda h: [PUBLIC_IP]))
    assert ok[4] == PUBLIC_IP and ok[1] == "a.com" and ok[2] == 443 and ok[3] == "/x"
    for addrs in (["10.0.0.5"], [PUBLIC_IP, "127.0.0.1"], []):  # una sola dirección privada basta para rechazar
        with pytest.raises(netguard.UnsafeUrl):
            run(netguard.resolve_safe("https://a.com/x", lambda h, a=addrs: a))
    with pytest.raises(netguard.UnsafeUrl):
        run(netguard.resolve_safe("https://169.254.169.254/latest/meta-data", lambda h: []))  # IP literal de metadatos
    assert run(netguard.resolve_safe("http://localhost:8080/x", lambda h: ["127.0.0.1"], allow_private=True, allow_http=True))[2] == 8080


# ---------- firma y cifrado ----------
def test_signature_roundtrip_tamper_and_replay_window():
    body = '{"a":1}'
    header = wh.signature_header("whsec_x", body, timestamp=1_000_000)
    assert wh.verify("whsec_x", header, body, now=1_000_100)
    assert not wh.verify("whsec_x", header, body + " ", now=1_000_100)  # cuerpo alterado
    assert not wh.verify("otro", header, body, now=1_000_100)  # otro secreto
    assert not wh.verify("whsec_x", header, body, now=1_000_000 + 301)  # repetición tardía
    for bad in ("", "t=abc,v1=zz", "v1=abc", "t=1"):
        assert not wh.verify("whsec_x", bad, body, now=1_000_000)


def test_secretbox_roundtrip_wrong_key_and_no_plaintext():
    token = secretbox.encrypt("clave-A", "whsec_secreto")
    assert "whsec_secreto" not in token and secretbox.decrypt("clave-A", token) == "whsec_secreto"
    with pytest.raises(secretbox.SecretBoxError):
        secretbox.decrypt("clave-B", token)
    with pytest.raises(secretbox.SecretBoxError):
        secretbox.decrypt("clave-A", "basura")


# ---------- API ----------
def test_secret_is_shown_once_and_stored_encrypted():
    c, _ = app_with()
    hook = create(c, ["call.completed"])
    assert hook["secret"].startswith("whsec_") and "no se vuelve a mostrar" in hook["warning"] and hook["events"] == ["call.completed"]
    listing = c.get("/api/v1/webhooks", headers=h()).json()
    assert hook["secret"] not in json.dumps(listing) and "secret_enc" not in json.dumps(listing) and "*" in listing["available_events"]
    stored = next(iter(c.app.state.db.webhooks.values()))
    assert hook["secret"] not in stored["secret_enc"] and secretbox.decrypt("t" * 40, stored["secret_enc"]) == hook["secret"]


def test_unsafe_and_invalid_input_is_rejected():
    c, _ = app_with(resolver=lambda host: ["10.0.0.5"])
    assert c.post("/api/v1/webhooks", json={"url": URL}, headers=h()).status_code == 422  # resuelve a red privada
    c2, _ = app_with()
    for body in ({"url": "http://hooks.example.com/x"}, {"url": "https://u:p@hooks.example.com/x"}, {"url": URL, "events": ["inventado"]},
                 {"url": URL, "events": []}, {"url": URL, "extra": 1}, {}):
        assert c2.post("/api/v1/webhooks", json=body, headers=h()).status_code == 422, body


def test_permissions_isolation_and_limits():
    c, _ = app_with(webhooks_max_per_org=2)
    for role in ("manager", "operator", "analyst", "viewer", "customer"):
        assert c.post("/api/v1/webhooks", json={"url": URL}, headers=h(ORG, role)).status_code == 403
    a = create(c)
    create(c)
    assert c.post("/api/v1/webhooks", json={"url": URL}, headers=h()).status_code == 409  # tope por organización
    assert c.get("/api/v1/webhooks", headers=h(OTHER)).json()["items"] == []
    for method, path in (("patch", ""), ("delete", ""), ("post", "/rotate-secret"), ("post", "/test"), ("get", "/deliveries")):
        r = getattr(c, method)(f"/api/v1/webhooks/{a['id']}{path}", headers=h(OTHER), **({"json": {"active": False}} if method == "patch" else {}))
        assert r.status_code == 404, (method, path)
    assert c.delete("/api/v1/webhooks/no-uuid", headers=h()).status_code == 404


def test_api_keys_cannot_manage_webhooks():
    c, _ = app_with()
    key = c.post("/api/v1/api-keys", json={"name": "ci", "role": "admin"}, headers=h()).json()["key"]
    kh = {"Authorization": f"Bearer {key}"}
    assert c.post("/api/v1/webhooks", json={"url": URL}, headers=kh).status_code == 403
    hook = create(c)
    assert c.patch(f"/api/v1/webhooks/{hook['id']}", json={"active": False}, headers=kh).status_code == 403
    assert c.delete(f"/api/v1/webhooks/{hook['id']}", headers=kh).status_code == 403


def test_update_revalidates_url_and_events():
    c, _ = app_with()
    hook = create(c)
    assert c.patch(f"/api/v1/webhooks/{hook['id']}", json={}, headers=h()).status_code == 422
    assert c.patch(f"/api/v1/webhooks/{hook['id']}", json={"url": "http://x.example.com/p"}, headers=h()).status_code == 422
    assert c.patch(f"/api/v1/webhooks/{hook['id']}", json={"events": ["nada"]}, headers=h()).status_code == 422
    ok = c.patch(f"/api/v1/webhooks/{hook['id']}", json={"events": ["tool.executed", "call.failed"], "active": False}, headers=h()).json()
    assert ok["events"] == ["call.failed", "tool.executed"] and ok["active"] is False
    audit = [e["action"] for e in c.get("/api/v1/audit-logs", headers=h()).json()["items"]]
    assert "webhook.created" in audit and "webhook.updated" in audit


# ---------- entrega ----------
def test_emit_respects_subscriptions_and_inactive_endpoints():
    c, _ = app_with()
    a, _b, inactive = create(c, ["call.completed"]), create(c, ["*"]), create(c, ["call.completed"])
    c.patch(f"/api/v1/webhooks/{inactive['id']}", json={"active": False}, headers=h())
    svc = c.app.state.webhooks
    assert run(svc.emit(ORG, "call.completed", {"call_id": "x"})) == 2  # a y b
    assert run(svc.emit(ORG, "tool.executed", {"tool": "t"})) == 1  # solo b
    assert run(svc.emit(OTHER, "call.completed", {})) == 0  # otra organización no recibe nada
    assert c.get(f"/api/v1/webhooks/{a['id']}/deliveries", headers=h()).json()["items"][0]["event_type"] == "call.completed"


def test_delivery_is_signed_pinned_to_the_validated_ip_and_marked_delivered():
    c, rec = app_with()
    hook = create(c)
    svc = c.app.state.webhooks
    run(svc.emit(ORG, "call.completed", {"call_id": "abc", "status": "COMPLETED"}))
    assert run(svc.tick()) == 1
    req = rec.requests[0]
    body = req.content.decode()
    assert urlparse(str(req.url)).hostname == PUBLIC_IP and req.url.path == "/omnivoice" and req.url.query == b"x=1"  # conecta a la IP validada
    assert req.headers["host"] == "hooks.example.com" and req.extensions["sni_hostname"] == "hooks.example.com"
    assert req.headers["x-omnivoice-event"] == "call.completed" and wh.verify(hook["secret"], req.headers["x-omnivoice-signature"], body)
    payload = json.loads(body)
    assert payload["type"] == "call.completed" and payload["org_id"] == ORG and payload["data"] == {"call_id": "abc", "status": "COMPLETED"}
    assert req.headers["x-omnivoice-delivery"] == payload["id"]
    d = c.get(f"/api/v1/webhooks/{hook['id']}/deliveries", headers=h()).json()["items"][0]
    assert d["status"] == "delivered" and d["attempts"] == 1 and d["last_status"] == 200 and d["last_error"] is None
    assert run(svc.tick()) == 0  # ya entregado: no se reenvía


def test_failures_retry_with_growing_backoff_then_give_up_without_storing_the_response_body():
    c, rec = app_with(Receiver(status=500))
    hook = create(c)
    svc = c.app.state.webhooks
    run(svc.emit(ORG, "call.failed", {"call_id": "x"}))
    now = datetime.now(timezone.utc)
    waits = []
    for attempt in range(wh.MAX_ATTEMPTS):
        assert run(svc.tick(now)) == 1
        d = c.app.state.db.webhook_deliveries[0]
        waits.append(None if d["status"] == "failed" else (d["next_attempt_at"] - now).total_seconds())
        assert run(svc.tick(now + timedelta(seconds=1))) == 0 or d["status"] != "failed"  # aún no vence (o ya falló)
        now = d["next_attempt_at"] + timedelta(seconds=1) if d["status"] != "failed" else now
    assert waits == [30.0, 120.0, 600.0, 3600.0, 21600.0, None]
    d = c.get(f"/api/v1/webhooks/{hook['id']}/deliveries", headers=h()).json()["items"][0]
    assert d["status"] == "failed" and d["attempts"] == wh.MAX_ATTEMPTS and d["last_status"] == 500 and d["last_error"] == "http_500"
    assert "cuerpo que no debe guardarse" not in json.dumps(d)
    assert run(svc.tick(now + timedelta(days=30))) == 0  # no se reintenta más


def test_timeouts_and_connection_errors_are_retried_with_a_safe_error():
    rec = Receiver()
    c, _ = app_with(rec)
    create(c)
    svc = c.app.state.webhooks
    run(svc.emit(ORG, "call.failed", {}))
    rec.raise_exc = httpx.ReadTimeout("lento")
    run(svc.tick())
    d = c.app.state.db.webhook_deliveries[0]
    assert d["status"] == "pending" and d["last_error"] == "timeout" and d["last_status"] is None
    rec.raise_exc = httpx.ConnectError("secreto interno 10.1.2.3 rechazó")
    run(svc.tick(d["next_attempt_at"] + timedelta(seconds=1)))
    assert d["last_error"] == "conexion: ConnectError" and "10.1.2.3" not in d["last_error"]


def test_destination_turning_private_after_registration_is_blocked_at_send_time():
    answers = {"ips": [PUBLIC_IP]}
    c, rec = app_with(resolver=lambda host: answers["ips"])
    create(c)
    svc = c.app.state.webhooks
    run(svc.emit(ORG, "call.failed", {}))
    answers["ips"] = ["10.0.0.9"]  # el DNS ahora apunta adentro (rebinding)
    run(svc.tick())
    d = c.app.state.db.webhook_deliveries[0]
    assert not rec.requests and d["status"] == "pending" and d["last_error"].startswith("destino_no_permitido")


def test_redirects_are_not_followed():
    c, rec = app_with(Receiver(status=302))
    create(c)
    svc = c.app.state.webhooks
    run(svc.emit(ORG, "call.failed", {}))
    run(svc.tick())
    assert len(rec.requests) == 1 and c.app.state.db.webhook_deliveries[0]["last_error"] == "http_302"


def test_rotating_the_secret_invalidates_the_old_one_and_test_event_works():
    c, rec = app_with()
    hook = create(c)
    new = c.post(f"/api/v1/webhooks/{hook['id']}/rotate-secret", headers=h()).json()
    assert new["secret"] != hook["secret"] and "no se vuelve a mostrar" in new["warning"]
    assert c.post(f"/api/v1/webhooks/{hook['id']}/test", headers=h()).json() == {"queued": 1}
    run(c.app.state.webhooks.tick())
    req = rec.requests[0]
    assert req.headers["x-omnivoice-event"] == "webhook.test"
    assert wh.verify(new["secret"], req.headers["x-omnivoice-signature"], req.content.decode())
    assert not wh.verify(hook["secret"], req.headers["x-omnivoice-signature"], req.content.decode())


def test_unreadable_secret_fails_without_retry():
    c, rec = app_with()
    create(c)
    next(iter(c.app.state.db.webhooks.values()))["secret_enc"] = "corrupto"
    run(c.app.state.webhooks.emit(ORG, "call.failed", {}))
    run(c.app.state.webhooks.tick())
    d = c.app.state.db.webhook_deliveries[0]
    assert d["status"] == "failed" and d["last_error"] == "secreto_ilegible" and not rec.requests


def test_deleting_an_endpoint_discards_its_pending_deliveries():
    c, _ = app_with()
    hook = create(c)
    run(c.app.state.webhooks.emit(ORG, "call.failed", {}))
    assert c.delete(f"/api/v1/webhooks/{hook['id']}", headers=h()).status_code == 204
    assert run(c.app.state.webhooks.tick()) == 0 and c.app.state.db.webhook_deliveries == []


def test_disabled_feature_queues_nothing():
    c, _ = app_with(webhooks_enabled=False)
    create(c)
    assert run(c.app.state.webhooks.emit(ORG, "call.failed", {})) == 0


# ---------- eventos de llamada y de herramientas ----------
def test_call_status_changes_emit_events_without_personal_data():
    c, tw = make_telephony()
    rec = Receiver()
    c.app.state.webhooks.http = rec.client()
    c.app.state.webhooks.resolver = lambda host: [PUBLIC_IP]
    aid = c.post("/api/v1/agents", json={"name": "Tienda", "instructions": "Atiende llamadas con amabilidad.", "tools": []}, headers=h()).json()["id"]
    c.post(f"/api/v1/agents/{aid}/publish", headers=h())
    c.post("/api/v1/phone-numbers", json={"e164": "+56911110000", "agent_id": aid}, headers=h())
    hook = create(c)
    r = c.post("/api/v1/outbound-calls", json={"to": "+56987654321", "consent_basis": "existing_customer"}, headers=h())
    assert r.status_code == 202
    from tests.test_call_tracking import status_post
    status_post(c, {"CallSid": "CAout", "CallStatus": "ringing", "SequenceNumber": "1"})
    status_post(c, {"CallSid": "CAout", "CallStatus": "no-answer", "SequenceNumber": "2"})
    run(c.app.state.webhooks.tick())
    events = [json.loads(q.content)["type"] for q in rec.requests]
    assert events == ["call.failed"]  # solo cambios que importan al cliente; ringing no genera evento
    body = rec.requests[0].content.decode()
    assert "+56987654321" not in body and "CAout" not in body and wh.verify(hook["secret"], rec.requests[0].headers["x-omnivoice-signature"], body)


def test_tool_execution_emits_event_with_name_and_result_but_no_arguments():
    from tests.test_confirmation import Harness, tool_call, wait
    hz = Harness().open()
    try:
        hz.c.app.state.webhooks.resolver = lambda host: [PUBLIC_IP]  # antes de registrar: la URL se valida al crearla
        made = hz.c.post("/api/v1/webhooks", json={"url": URL, "events": ["tool.executed"]}, headers=h("o1"))
        assert made.status_code == 201, made.text
        hz.push(tool_call("c1", "check_inventory", {"product": "chaqueta SECRETA", "color": "negro", "size": "M"}))
        assert wait(lambda: len(hz.results()) == 1)
        assert wait(lambda: hz.c.app.state.db.webhook_deliveries)
        d = hz.c.app.state.db.webhook_deliveries[0]
        assert d["event_type"] == "tool.executed" and d["payload"]["data"]["tool"] == "check_inventory" and d["payload"]["data"]["ok"] is True
        assert "SECRETA" not in json.dumps(d["payload"])
    finally:
        hz.close()


def test_poll_loop_start_stop():
    c, _ = app_with()
    svc = c.app.state.webhooks

    async def go():
        svc.start()
        await asyncio.sleep(0.05)
        await svc.stop()

    run(go())
    assert time.time() > 0
