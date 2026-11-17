"""Etapa 8/10 contra PostgreSQL real: claves de API (migración 011): resolución global exacta, RLS, revocación y vencimiento."""
import asyncio
import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import Settings, get_settings
from app.main import create_app
from app.security.auth import issue_token

PG_URL = os.environ.get("TEST_DATABASE_URL")
REDIS_URL = os.environ.get("TEST_REDIS_URL")
ADMIN_URL = os.environ.get("TEST_ADMIN_DATABASE_URL")
DEMO = "00000000-0000-0000-0000-000000000001"
OTHER = "00000000-0000-0000-0000-0000000000ee"

pytestmark = pytest.mark.skipif(not (PG_URL and REDIS_URL), reason="requiere TEST_DATABASE_URL y TEST_REDIS_URL")


def _jwt(org, role="admin"):
    return {"Authorization": f"Bearer {issue_token('u-key', org, role)}"}


def _bearer(key):
    return {"Authorization": f"Bearer {key}"}


def _settings():
    return Settings(environment="test", jwt_secret=get_settings().jwt_secret, persistence_backend="postgres", state_backend="redis",
                    database_url=PG_URL, redis_url=REDIS_URL, vad_backend="energy", retention_job_enabled=False)


def test_api_key_lifecycle_on_postgres():
    with TestClient(create_app(_settings())) as c:
        k = c.post("/api/v1/api-keys", headers=_jwt(DEMO), json={"name": "integración pg", "role": "analyst"}).json()
        assert k["key"].startswith("ovk_")

        assert c.get("/api/v1/agents", headers=_bearer(k["key"])).status_code == 200  # resuelve por prefijo y verifica el hash
        assert c.get("/api/v1/phone-numbers", headers=_bearer(k["key"])).status_code == 200  # analyst lee números
        assert c.post("/api/v1/agents", headers=_bearer(k["key"]), json={
            "name": "x" * 3, "instructions": "Atiende llamadas con amabilidad.", "tools": []}).status_code == 403
        assert c.get("/api/v1/agents", headers=_bearer(k["key"][:-1] + "x")).status_code == 401  # alterada

        listed = [i for i in c.get("/api/v1/api-keys", headers=_jwt(DEMO)).json()["items"] if i["id"] == k["id"]]
        assert listed and listed[0]["last_used_at"] is not None and k["key"] not in str(listed)
        assert c.get("/api/v1/api-keys", headers=_jwt(OTHER)).json()["items"] == []  # RLS
        assert c.delete(f"/api/v1/api-keys/{k['id']}", headers=_jwt(OTHER)).status_code == 404

        assert c.delete(f"/api/v1/api-keys/{k['id']}", headers=_jwt(DEMO)).status_code == 204
        assert c.get("/api/v1/agents", headers=_bearer(k["key"])).status_code == 401  # revocada


def test_expired_key_is_rejected_and_table_holds_only_the_hash():
    with TestClient(create_app(_settings())) as c:
        k = c.post("/api/v1/api-keys", headers=_jwt(DEMO), json={"name": "vence pronto", "role": "viewer", "expires_in_days": 1}).json()
        assert c.get("/api/v1/agents", headers=_bearer(k["key"])).status_code == 200

        async def expire_and_inspect():
            eng = create_async_engine(ADMIN_URL)
            try:
                async with eng.begin() as conn:
                    await conn.execute(text("UPDATE api_keys SET expires_at = now() - interval '1 second' WHERE id = :i"), {"i": k["id"]})
                    row = (await conn.execute(text("SELECT key_hash, prefix FROM api_keys WHERE id = :i"), {"i": k["id"]})).first()
                return row
            finally:
                await eng.dispose()

        if not ADMIN_URL:
            pytest.skip("requiere TEST_ADMIN_DATABASE_URL")
        row = asyncio.run(expire_and_inspect())
        assert k["key"] not in row[0] and len(row[0]) == 64 and row[1] == k["prefix"]  # solo hash SHA-256 en hexadecimal
        assert c.get("/api/v1/agents", headers=_bearer(k["key"])).status_code == 401
        c.delete(f"/api/v1/api-keys/{k['id']}", headers=_jwt(DEMO))


def test_resolver_function_is_exact_and_table_stays_isolated():
    async def scenario(c):
        eng = create_async_engine(PG_URL)
        try:
            async with eng.begin() as conn:
                via_fn = (await conn.execute(text("SELECT count(*) FROM resolve_api_key('00000000')"))).scalar()  # prefijo inexistente
            try:  # sin app.org_id la política falla cerrado: error o cero filas, nunca datos
                async with eng.begin() as conn:
                    direct = (await conn.execute(text("SELECT count(*) FROM api_keys"))).scalar()
            except Exception:  # noqa: BLE001
                direct = 0
            return via_fn, direct
        finally:
            await eng.dispose()

    with TestClient(create_app(_settings())) as c:
        c.post("/api/v1/api-keys", headers=_jwt(DEMO), json={"name": "existe", "role": "viewer"})
    assert asyncio.run(scenario(None)) == (0, 0)
