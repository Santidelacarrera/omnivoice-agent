"""El SDK de Python contra la API real (en memoria) y la función de verificación de webhooks."""
import sys
import time
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "sdk" / "python"))
from omnivoice_sdk import OmniVoice, OmniVoiceError, verify_webhook  # noqa: E402

from app.security.auth import issue_token  # noqa: E402
from app.services import webhooks as wh  # noqa: E402
from tests.test_api import make_client  # noqa: E402
from tests.test_telephony import make as make_telephony  # noqa: E402

ORG = str(uuid.uuid4())


def sdk(c):
    """El TestClient de Starlette es un cliente httpx: el SDK lo usa tal cual, sin red."""
    key = c.post("/api/v1/api-keys", json={"name": "sdk", "role": "admin"},
                 headers={"Authorization": f"Bearer {issue_token('u', ORG, 'admin')}"}).json()["key"]
    return OmniVoice("http://testserver", api_key=key, http_client=c)


def test_agents_lifecycle_through_the_sdk():
    c = make_client()
    ov = sdk(c)
    a = ov.agents.create(name="Recepción", instructions="Atiende llamadas con amabilidad.", tools=["check_inventory"])
    assert ov.agents.update(a["id"], name="Recepción 2")["version"] == 2
    assert [v["version"] for v in ov.agents.versions(a["id"])] == [1, 2]
    assert ov.agents.rollback(a["id"], 1)["name"] == "Recepción"
    assert ov.agents.publish(a["id"])["status"] == "published"
    assert [x["id"] for x in ov.agents.list()] == [a["id"]] and ov.agents.get(a["id"])["status"] == "published"
    assert ov.agents.archive(a["id"])["status"] == "archived"


def test_calls_outbound_analytics_and_errors():
    c, tw = make_telephony()
    ov = sdk(c)
    with pytest.raises(OmniVoiceError) as e:  # sin consentimiento no hay llamada
        ov.calls.create(to="+56987654321", consent_basis="nada")
    assert e.value.status == 422
    with pytest.raises(OmniVoiceError) as e:  # sin número con agente publicado
        ov.calls.create(to="+56987654321", consent_basis="existing_customer")
    assert e.value.status == 422 and "número" in e.value.message
    a = ov.agents.create(name="Tienda", instructions="Atiende llamadas con amabilidad.")
    ov.agents.publish(a["id"])
    c.post("/api/v1/phone-numbers", json={"e164": "+56911110000", "agent_id": a["id"]},
           headers={"Authorization": f"Bearer {issue_token('u', ORG, 'admin')}"})
    assert ov.phone_numbers()[0]["e164"] == "+56911110000"
    job = ov.calls.create(to="+56987654321", consent_basis="existing_customer", idempotency_key="sdk-0001")
    assert job["status"] == "DIALING" and job["call_sid"] == "CAout" and len(tw.calls) == 1  # la ventana de las pruebas está abierta
    replay = ov.calls.create(to="+56987654321", consent_basis="existing_customer", idempotency_key="sdk-0001")
    assert replay["replayed"] is True and replay["id"] == job["id"]  # misma clave: no se crea otra llamada
    assert ov.outbound.get(job["id"])["id"] == job["id"] and len(ov.outbound.list()) == 1
    assert [x["direction"] for x in ov.calls.list()] == ["outbound"]
    assert ov.analytics(days=7)["calls"]["total"] == 1 and ov.analytics(days=7)["period_days"] == 7 and ov.usage()["period_days"] == 30
    with pytest.raises(OmniVoiceError) as e:  # el número ya tiene un trabajo activo
        ov.calls.create(to="+56987654321", consent_basis="transactional")
    assert e.value.status == 409
    assert ov.outbound.do_not_call("+56955550000", "pidió no ser llamado")["created"] is True
    with pytest.raises(OmniVoiceError) as e:
        ov.calls.create(to="+56955550000", consent_basis="existing_customer")
    assert e.value.status == 403


def test_keys_cannot_manage_webhooks_through_the_sdk():
    c = make_client()
    ov = sdk(c)
    with pytest.raises(OmniVoiceError) as e:
        ov.webhooks.create("https://hooks.example.com/x")
    assert e.value.status == 403


def test_verify_webhook_matches_the_server_side_signature():
    body, secret = '{"id":"1"}', "whsec_x"
    header = wh.signature_header(secret, body, timestamp=1_000_000)
    assert verify_webhook(secret, header, body, now=1_000_100)
    assert not verify_webhook(secret, header, body + " ", now=1_000_100)
    assert not verify_webhook("otro", header, body, now=1_000_100)
    assert not verify_webhook(secret, header, body, now=1_000_000 + 301)
    assert not verify_webhook(secret, "basura", body)
    assert verify_webhook(secret, wh.signature_header(secret, body), body)  # con el reloj real
    assert time.time() > 0


def test_calls_alias_equals_outbound_calls_and_includes_call_sid():
    c, _ = make_telephony()
    hdr = {"Authorization": f"Bearer {issue_token('u', ORG, 'admin')}"}
    a = c.post("/api/v1/agents", json={"name": "Tienda", "instructions": "Atiende llamadas con amabilidad.", "tools": []}, headers=hdr).json()
    c.post(f"/api/v1/agents/{a['id']}/publish", headers=hdr)
    c.post("/api/v1/phone-numbers", json={"e164": "+56911110000", "agent_id": a["id"]}, headers=hdr)
    r = c.post("/api/v1/calls", json={"to": "+56987654321", "consent_basis": "existing_customer"}, headers=hdr)
    assert r.status_code == 202 and "call_sid" in r.json() and r.json()["status"] in ("SCHEDULED", "DIALING")
    assert c.post("/api/v1/calls", json={"to": "+56987654321"}, headers=hdr).status_code == 422
    assert c.post("/api/v1/calls", json={"to": "+56987654321", "consent_basis": "existing_customer"},
                  headers={"Authorization": f"Bearer {issue_token('u', ORG, 'viewer')}"}).status_code == 403
