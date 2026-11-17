"""Migración 018 contra PostgreSQL real: las reglas de equipo (último owner, owner_only) viven en la base de datos."""
import asyncio
import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import Settings, get_settings
from app.main import create_app
from app.security.auth import issue_token
from app.services import provisioning as pv

PG_URL = os.environ.get("TEST_DATABASE_URL")
REDIS_URL = os.environ.get("TEST_REDIS_URL")
ADMIN_URL = os.environ.get("TEST_ADMIN_DATABASE_URL")

pytestmark = pytest.mark.skipif(not (PG_URL and REDIS_URL and ADMIN_URL), reason="requiere TEST_DATABASE_URL, TEST_REDIS_URL y TEST_ADMIN_DATABASE_URL")


def _h(org, role):
    return {"Authorization": f"Bearer {issue_token('u-team', org, role)}"}


def _provision():
    async def main():
        eng = create_async_engine(ADMIN_URL)
        try:
            return await pv.create_organization(eng, "Org de equipo", "owner-team@example.com")
        finally:
            await eng.dispose()

    return asyncio.run(main())["org_id"]


def test_team_rules_enforced_by_the_database():
    org = _provision()
    s = Settings(environment="test", jwt_secret=get_settings().jwt_secret, persistence_backend="postgres", state_backend="redis",
                 database_url=PG_URL, redis_url=REDIS_URL, vad_backend="energy", retention_job_enabled=False)
    with TestClient(create_app(s)) as c:
        items = c.get("/api/v1/team", headers=_h(org, "admin")).json()["items"]
        assert [(m["email"], m["role"]) for m in items] == [("owner-team@example.com", "owner")]

        assert c.put("/api/v1/team/Ana@Example.com", headers=_h(org, "admin"), json={"role": "operator"}).status_code == 200
        assert c.put("/api/v1/team/ana@example.com", headers=_h(org, "admin"), json={"role": "owner"}).status_code == 403
        assert c.put("/api/v1/team/owner-team@example.com", headers=_h(org, "owner"), json={"role": "admin"}).status_code == 409
        assert c.delete("/api/v1/team/owner-team@example.com", headers=_h(org, "owner")).status_code == 409
        assert c.put("/api/v1/team/ana@example.com", headers=_h(org, "owner"), json={"role": "owner"}).status_code == 200
        assert c.delete("/api/v1/team/owner-team@example.com", headers=_h(org, "admin")).status_code == 403
        assert c.delete("/api/v1/team/owner-team@example.com", headers=_h(org, "owner")).status_code == 204
        assert c.delete("/api/v1/team/owner-team@example.com", headers=_h(org, "owner")).status_code == 404
        assert c.put("/api/v1/team/x@example.com", headers=_h(org, "admin"), json={"role": "viewer"}).status_code == 200
        assert c.put("/api/v1/team/no-email", headers=_h(org, "admin"), json={"role": "viewer"}).status_code == 422

        # otra organización no ve estos miembros
        other = _provision()
        assert {m["email"] for m in c.get("/api/v1/team", headers=_h(other, "admin")).json()["items"]} == {"owner-team@example.com"}
