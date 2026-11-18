"""Migración 019 contra PostgreSQL real: integraciones por organización (RLS), nombre único, credencial cifrada y solo accesible por el servicio."""
import os
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import Settings, get_settings
from app.main import create_app
from app.security.auth import issue_token

PG_URL = os.environ.get("TEST_DATABASE_URL")
REDIS_URL = os.environ.get("TEST_REDIS_URL")
DEMO = "00000000-0000-0000-0000-000000000001"
OTHER = "00000000-0000-0000-0000-0000000000ee"

pytestmark = pytest.mark.skipif(not (PG_URL and REDIS_URL), reason="requiere TEST_DATABASE_URL y TEST_REDIS_URL")


def _h(org, role="admin"):
    return {"Authorization": f"Bearer {issue_token('u-int', org, role)}"}


def test_integrations_on_postgres_with_rls_and_encrypted_credentials():
    seen = []

    def remote(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    s = Settings(environment="test", jwt_secret=get_settings().jwt_secret, persistence_backend="postgres", state_backend="redis",
                 database_url=PG_URL, redis_url=REDIS_URL, vad_backend="energy", retention_job_enabled=False, webhook_secret_key="k" * 32)
    name = "crm-" + uuid.uuid4().hex[:8]
    body = {"name": name, "base_url": "https://crm.example.com/api", "auth_type": "bearer", "credential": {"token": "pg-secret-token"},
            "methods": ["GET"], "paths": ["/orders"]}
    with TestClient(create_app(s, integrations_http=httpx.AsyncClient(transport=httpx.MockTransport(remote)), webhook_resolver=lambda h: ["93.184.216.34"])) as c:
        r = c.post("/api/v1/integrations", json=body, headers=_h(DEMO))
        assert r.status_code == 201 and r.json()["has_credential"] is True and "pg-secret-token" not in r.text
        assert c.post("/api/v1/integrations", json=body, headers=_h(DEMO)).status_code == 409  # UNIQUE (org_id, name)
        assert c.post(f"/api/v1/integrations/{name}/test", json={"path": "/orders/1"}, headers=_h(DEMO)).json() == {"status": 200, "ok": True}
        assert seen[-1].headers["Authorization"] == "Bearer pg-secret-token"  # descifrada solo para llamar

        # la base de datos solo guarda texto cifrado
        async def stored():
            from app.database.engine import org_transaction

            async with org_transaction(DEMO) as conn:
                return (await conn.execute(text("SELECT credential_enc FROM integrations WHERE name = :n"), {"n": name})).scalar()

        enc = c.portal.call(stored)
        assert enc.startswith("gAAAA") and "pg-secret-token" not in enc

        # RLS: otra organización no la ve ni la toca
        assert name not in [i["name"] for i in c.get("/api/v1/integrations", headers=_h(OTHER)).json()["items"]]
        assert c.patch(f"/api/v1/integrations/{name}", json={"enabled": False}, headers=_h(OTHER)).status_code == 404
        assert c.delete(f"/api/v1/integrations/{name}", headers=_h(OTHER)).status_code == 404
        assert c.post(f"/api/v1/integrations/{name}/test", json={"path": "/orders/1"}, headers=_h(OTHER)).status_code == 404

        up = c.patch(f"/api/v1/integrations/{name}", json={"enabled": False, "paths": ["/orders", "/customers"]}, headers=_h(DEMO))
        assert up.status_code == 200 and up.json()["enabled"] is False and up.json()["allowed_paths"] == ["/orders", "/customers"]
        assert c.delete(f"/api/v1/integrations/{name}", headers=_h(DEMO)).status_code == 204
        assert c.delete(f"/api/v1/integrations/{name}", headers=_h(DEMO)).status_code == 404


def test_app_role_cannot_bypass_rls_on_integrations():
    import asyncio

    async def attempt():
        eng = create_async_engine(PG_URL)
        try:
            async with eng.begin() as conn:
                await conn.execute(text("SELECT set_config('app.org_id', :o, true)"), {"o": OTHER})
                return (await conn.execute(text("SELECT count(*) FROM integrations WHERE org_id = :d"), {"d": DEMO})).scalar()
        finally:
            await eng.dispose()

    assert asyncio.run(attempt()) == 0
