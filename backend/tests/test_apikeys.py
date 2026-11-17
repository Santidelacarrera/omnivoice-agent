"""Etapa 8/10: claves de API (creación única, roles acotados, revocación, vencimiento, aislamiento)."""
import json
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.security import apikeys
from app.security.auth import issue_token
from tests.test_api import make_client

ORG, OTHER = str(uuid.uuid4()), str(uuid.uuid4())
BODY = {"name": "Agente", "instructions": "Atiende llamadas con amabilidad.", "tools": []}


def jwt(org=ORG, role="admin"):
    return {"Authorization": f"Bearer {issue_token('u', org, role)}"}


def bearer(key):
    return {"Authorization": f"Bearer {key}"}


def create(c, role="viewer", org=ORG, **kw):
    r = c.post("/api/v1/api-keys", json={"name": "integración", "role": role, **kw}, headers=jwt(org))
    assert r.status_code == 201, r.text
    return r.json()


# ---------- módulo ----------
def test_key_format_hash_and_verification():
    key, prefix, h = apikeys.generate()
    assert key.startswith("ovk_" + prefix + "_") and len(prefix) == 8 and h == apikeys.hash_key(key) and key not in h
    assert apikeys.parse_prefix(key) == prefix
    assert apikeys.verify(key, h, None, False)
    assert not apikeys.verify(key + "x", h, None, False)
    assert not apikeys.verify(key, h, None, True)  # revocada
    assert not apikeys.verify(key, h, time.time() - 1, False)  # vencida
    assert apikeys.verify(key, h, time.time() + 60, False)


@pytest.mark.parametrize("bad", ["", "ovk_", "ovk_zzzzzzzz_abc", "ovk_12345678", "ovk_1234567_abc", "ovk_12345678_", "xyz_12345678_abc", "ovk_" + "a" * 200])
def test_malformed_keys_are_rejected(bad):
    assert apikeys.parse_prefix(bad) is None


# ---------- API ----------
def test_key_is_shown_once_and_never_stored_in_clear():
    c = make_client()
    k = create(c)
    assert k["key"].startswith("ovk_") and "no se vuelve a mostrar" in k["warning"]
    listing = c.get("/api/v1/api-keys", headers=jwt()).json()["items"]
    assert len(listing) == 1 and k["key"] not in json.dumps(listing) and "key_hash" not in json.dumps(listing)
    assert listing[0]["prefix"] == k["prefix"] and listing[0]["role"] == "viewer"
    stored = next(iter(c.app.state.db.api_keys.values()))
    assert k["key"] not in json.dumps(stored, default=str) and stored["key_hash"] == apikeys.hash_key(k["key"])


def test_key_authenticates_with_its_role_and_org():
    c = make_client()
    viewer, admin = create(c, "viewer"), create(c, "admin")
    assert c.get("/api/v1/agents", headers=bearer(viewer["key"])).status_code == 200
    assert c.post("/api/v1/agents", json=BODY, headers=bearer(viewer["key"])).status_code == 403  # solo lectura
    assert c.post("/api/v1/agents", json=BODY, headers=bearer(admin["key"])).status_code == 201
    assert c.get("/api/v1/agents", headers=bearer(viewer["key"])).json()["items"][0]["name"] == "Agente"
    audit = c.get("/api/v1/audit-logs", headers=jwt()).json()["items"]
    assert any(e["actor"] == f"apikey:{admin['id']}" and e["action"] == "agent.created" for e in audit)  # queda a nombre de la clave


def test_invalid_revoked_and_expired_keys_get_the_same_401():
    c = make_client()
    k = create(c, "viewer")
    for hdr in (bearer("ovk_deadbeef_noexiste"), bearer(k["key"] + "x"), bearer("ovk_zz")):
        r = c.get("/api/v1/agents", headers=hdr)
        assert r.status_code == 401 and r.json()["detail"] == "Credencial inválida"
    assert c.get("/api/v1/agents", headers={"Authorization": "Bearer nada"}).status_code == 401
    assert c.delete(f"/api/v1/api-keys/{k['id']}", headers=jwt()).status_code == 204
    revoked = c.get("/api/v1/agents", headers=bearer(k["key"]))
    assert revoked.status_code == 401 and revoked.json()["detail"] == "Credencial inválida"
    assert c.get("/api/v1/api-keys", headers=jwt()).json()["items"][0]["revoked"] is True
    assert c.delete(f"/api/v1/api-keys/{k['id']}", headers=jwt()).status_code == 204  # idempotente

    e = create(c, "viewer", expires_in_days=1)
    next(x for x in c.app.state.db.api_keys.values() if x["id"] == e["id"])["expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert c.get("/api/v1/agents", headers=bearer(e["key"])).status_code == 401


def test_creation_rules_roles_and_no_privilege_escalation():
    c = make_client()
    for role in ("viewer", "analyst", "operator", "manager"):
        assert c.post("/api/v1/api-keys", json={"name": "xxx"}, headers=jwt(ORG, role)).status_code == 403  # solo owner/admin
    assert c.post("/api/v1/api-keys", json={"name": "owner", "role": "owner"}, headers=jwt()).status_code == 422  # nunca owner
    assert c.post("/api/v1/api-keys", json={"name": "xxx", "expires_in_days": 0}, headers=jwt()).status_code == 422
    assert c.post("/api/v1/api-keys", json={"name": "xxx", "role": "viewer", "extra": 1}, headers=jwt()).status_code == 422
    assert c.post("/api/v1/api-keys", json={"name": "okk", "role": "admin"}, headers=jwt(ORG, "owner")).status_code == 201


def test_a_key_cannot_manage_keys():
    c = make_client()
    admin_key = create(c, "admin")
    assert c.post("/api/v1/api-keys", json={"name": "otra", "role": "viewer"}, headers=bearer(admin_key["key"])).status_code == 403
    assert c.get("/api/v1/api-keys", headers=bearer(admin_key["key"])).status_code == 403
    assert c.delete(f"/api/v1/api-keys/{admin_key['id']}", headers=bearer(admin_key["key"])).status_code == 403


def test_keys_are_isolated_between_orgs():
    c = make_client()
    k = create(c, "admin")
    other = create(c, "viewer", org=OTHER)
    assert c.get("/api/v1/api-keys", headers=jwt(OTHER)).json()["items"][0]["id"] == other["id"]
    assert c.delete(f"/api/v1/api-keys/{k['id']}", headers=jwt(OTHER)).status_code == 404
    assert c.get("/api/v1/agents", headers=bearer(k["key"])).status_code == 200  # sigue viva
    c.post("/api/v1/agents", json=BODY, headers=jwt())
    assert c.get("/api/v1/agents", headers=bearer(other["key"])).json()["items"] == []  # cada clave ve solo su organización


def test_last_used_is_recorded_without_exposing_secrets():
    c = make_client()
    k = create(c, "viewer")
    c.get("/api/v1/agents", headers=bearer(k["key"]))
    assert c.get("/api/v1/api-keys", headers=jwt()).json()["items"][0]["last_used_at"] is not None
