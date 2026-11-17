"""Etapa 2: roles completos, agentes versionados y números telefónicos (con aislamiento entre organizaciones)."""
import uuid

import pytest

from tests.test_api import auth, make_client

ORG_A, ORG_B = str(uuid.uuid4()), str(uuid.uuid4())
BODY = {"name": "Recepción", "instructions": "Atiende llamadas de la clínica con amabilidad.", "tools": ["check_inventory"]}


def mk(c, org=ORG_A, role="admin"):
    r = c.post("/api/v1/agents", json=BODY, headers=auth(role, org))
    assert r.status_code == 201, r.text
    return r.json()


@pytest.mark.parametrize("role,read,write,publish", [
    ("owner", 200, 201, 200), ("admin", 200, 201, 200), ("manager", 200, 201, 200),
    ("operator", 200, 403, 403), ("analyst", 200, 403, 403), ("viewer", 200, 403, 403), ("customer", 403, 403, 403),
])
def test_role_matrix(role, read, write, publish):
    c = make_client()
    agent = mk(c)
    h = auth(role, ORG_A)
    assert c.get("/api/v1/agents", headers=h).status_code == read
    assert c.post("/api/v1/agents", json=BODY, headers=h).status_code == write
    assert c.post(f"/api/v1/agents/{agent['id']}/publish", headers=h).status_code == publish


def test_viewer_cannot_see_audit_but_analyst_can():
    c = make_client()
    assert c.get("/api/v1/audit-logs", headers=auth("viewer")).status_code == 403
    assert c.get("/api/v1/audit-logs", headers=auth("analyst")).status_code == 200


def test_versioning_and_rollback():
    c = make_client()
    a = mk(c)
    assert a["version"] == 1 and a["status"] == "draft"
    h = auth("admin", ORG_A)
    r = c.patch(f"/api/v1/agents/{a['id']}", json={"name": "Recepción v2"}, headers=h)
    assert r.status_code == 200 and r.json()["version"] == 2 and r.json()["instructions"] == BODY["instructions"]
    versions = c.get(f"/api/v1/agents/{a['id']}/versions", headers=h).json()["items"]
    assert [v["version"] for v in versions] == [1, 2]
    rb = c.post(f"/api/v1/agents/{a['id']}/rollback", json={"version": 1}, headers=h).json()
    assert rb["name"] == "Recepción" and rb["version"] == 3  # el historial nunca se reescribe
    assert c.post(f"/api/v1/agents/{a['id']}/rollback", json={"version": 99}, headers=h).status_code == 404
    kinds = [e["action"] for e in c.get("/api/v1/audit-logs", headers=h).json()["items"]]
    assert "agent.updated" in kinds and "agent.rolled_back" in kinds


def test_patch_validation_and_archived():
    c = make_client()
    a = mk(c)
    h = auth("admin", ORG_A)
    assert c.patch(f"/api/v1/agents/{a['id']}", json={}, headers=h).status_code == 422
    assert c.patch(f"/api/v1/agents/{a['id']}", json={"tools": ["no_existe"]}, headers=h).status_code == 422
    assert c.patch(f"/api/v1/agents/{a['id']}", json={"rol": "x"}, headers=h).status_code == 422
    assert c.post(f"/api/v1/agents/{a['id']}/archive", headers=h).json()["status"] == "archived"
    assert c.patch(f"/api/v1/agents/{a['id']}", json={"name": "otro"}, headers=h).status_code == 409
    assert c.get("/api/v1/agents/no-es-uuid", headers=h).status_code == 404


def test_agents_isolated_between_orgs():
    c = make_client()
    a = mk(c, ORG_A)
    hb = auth("owner", ORG_B)
    aid = a["id"]
    assert c.get(f"/api/v1/agents/{aid}", headers=hb).status_code == 404
    assert c.patch(f"/api/v1/agents/{aid}", json={"name": "hack"}, headers=hb).status_code == 404
    assert c.get(f"/api/v1/agents/{aid}/versions", headers=hb).status_code == 404
    assert c.post(f"/api/v1/agents/{aid}/rollback", json={"version": 1}, headers=hb).status_code == 404
    assert c.post(f"/api/v1/agents/{aid}/publish", headers=hb).status_code == 404
    assert c.post(f"/api/v1/agents/{aid}/archive", headers=hb).status_code == 404
    assert c.get(f"/api/v1/agents/{aid}", headers=auth("owner", ORG_A)).json()["status"] == "draft"


def test_phone_numbers_crud_and_uniqueness():
    c = make_client()
    a = mk(c)
    h = auth("admin", ORG_A)
    r = c.post("/api/v1/phone-numbers", json={"e164": "+56912345678", "agent_id": a["id"]}, headers=h)
    assert r.status_code == 201
    assert c.post("/api/v1/phone-numbers", json={"e164": "+56912345678"}, headers=h).status_code == 409
    assert c.post("/api/v1/phone-numbers", json={"e164": "56912345678"}, headers=h).status_code == 422
    assert c.post("/api/v1/phone-numbers", json={"e164": "+56911111111", "agent_id": str(uuid.uuid4())}, headers=h).status_code == 404
    assert len(c.get("/api/v1/phone-numbers", headers=auth("analyst", ORG_A)).json()["items"]) == 1
    assert c.post("/api/v1/phone-numbers", json={"e164": "+56922222222"}, headers=auth("operator", ORG_A)).status_code == 403
    assert c.delete(f"/api/v1/phone-numbers/{r.json()['id']}", headers=h).status_code == 204
    assert c.delete(f"/api/v1/phone-numbers/{r.json()['id']}", headers=h).status_code == 404


def test_phone_numbers_isolated_between_orgs():
    c = make_client()
    a = mk(c, ORG_A)
    pa = c.post("/api/v1/phone-numbers", json={"e164": "+56933333333", "agent_id": a["id"]}, headers=auth("admin", ORG_A)).json()
    hb = auth("admin", ORG_B)
    assert c.get("/api/v1/phone-numbers", headers=hb).json()["items"] == []
    assert c.delete(f"/api/v1/phone-numbers/{pa['id']}", headers=hb).status_code == 404
    # B no puede registrar el mismo número ni asociar números a agentes de A
    assert c.post("/api/v1/phone-numbers", json={"e164": "+56933333333"}, headers=hb).status_code == 409
    assert c.post("/api/v1/phone-numbers", json={"e164": "+56944444444", "agent_id": a["id"]}, headers=hb).status_code == 404
