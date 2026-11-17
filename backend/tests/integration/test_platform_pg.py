"""Etapa 2 contra PostgreSQL real: versiones de agente, números telefónicos y RLS (migración 005)."""
import os
import uuid

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings, get_settings
from app.main import create_app
from app.security.auth import issue_token

PG_URL = os.environ.get("TEST_DATABASE_URL")
REDIS_URL = os.environ.get("TEST_REDIS_URL")
DEMO = "00000000-0000-0000-0000-000000000001"
OTHER = "00000000-0000-0000-0000-0000000000ee"

pytestmark = pytest.mark.skipif(not (PG_URL and REDIS_URL), reason="requiere TEST_DATABASE_URL y TEST_REDIS_URL")


def _h(org, role="admin"):
    return {"Authorization": f"Bearer {issue_token('u-pg', org, role)}"}


def test_agents_versions_and_phones_on_postgres():
    settings = Settings(environment="test", jwt_secret=get_settings().jwt_secret, persistence_backend="postgres",
                        state_backend="redis", database_url=PG_URL, redis_url=REDIS_URL, vad_backend="energy",
                        retention_job_enabled=False)
    number = "+569" + str(uuid.uuid4().int)[:8]
    with TestClient(create_app(settings)) as c:
        a = c.post("/api/v1/agents", headers=_h(DEMO), json={
            "name": "Clínica", "instructions": "Atiende pacientes con amabilidad.", "tools": ["check_inventory"]}).json()
        assert a["version"] == 1 and a["status"] == "draft"
        up = c.patch(f"/api/v1/agents/{a['id']}", headers=_h(DEMO), json={"name": "Clínica v2", "tools": ["search_knowledge_base"]})
        assert up.status_code == 200 and up.json()["version"] == 2 and up.json()["tools"] == ["search_knowledge_base"]
        vs = c.get(f"/api/v1/agents/{a['id']}/versions", headers=_h(DEMO, "analyst")).json()["items"]
        assert [v["version"] for v in vs] == [1, 2] and vs[0]["tools"] == ["check_inventory"]
        rb = c.post(f"/api/v1/agents/{a['id']}/rollback", headers=_h(DEMO, "manager"), json={"version": 1}).json()
        assert rb["version"] == 3 and rb["name"] == "Clínica" and rb["tools"] == ["check_inventory"]
        assert c.post(f"/api/v1/agents/{a['id']}/publish", headers=_h(DEMO)).json()["status"] == "published"

        # otra organización no ve ni toca nada (RLS)
        assert c.get(f"/api/v1/agents/{a['id']}", headers=_h(OTHER)).status_code == 404
        assert c.patch(f"/api/v1/agents/{a['id']}", headers=_h(OTHER), json={"name": "x" * 5}).status_code == 404

        p = c.post("/api/v1/phone-numbers", headers=_h(DEMO), json={"e164": number, "agent_id": a["id"]})
        assert p.status_code == 201, p.text
        assert c.post("/api/v1/phone-numbers", headers=_h(DEMO), json={"e164": number}).status_code == 409
        assert c.post("/api/v1/phone-numbers", headers=_h(OTHER), json={"e164": number}).status_code == 409
        assert c.get("/api/v1/phone-numbers", headers=_h(OTHER)).json()["items"] == []
        assert c.delete(f"/api/v1/phone-numbers/{p.json()['id']}", headers=_h(OTHER)).status_code == 404
        assert c.delete(f"/api/v1/phone-numbers/{p.json()['id']}", headers=_h(DEMO)).status_code == 204

        assert c.post(f"/api/v1/agents/{a['id']}/archive", headers=_h(DEMO)).json()["status"] == "archived"
        assert c.patch(f"/api/v1/agents/{a['id']}", headers=_h(DEMO), json={"name": "zzzzz"}).status_code == 409
