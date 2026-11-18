"""Cuotas del plan con PostgreSQL real y peticiones simultáneas: nunca se supera el límite."""
import asyncio
import os

import httpx
import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import Settings, get_settings
from app.main import create_app
from app.security.auth import issue_token
from app.services import provisioning as pv

PG_URL = os.environ.get("TEST_DATABASE_URL")
REDIS_URL = os.environ.get("TEST_REDIS_URL")
ADMIN_URL = os.environ.get("TEST_ADMIN_DATABASE_URL")

pytestmark = pytest.mark.skipif(not (PG_URL and REDIS_URL and ADMIN_URL), reason="requiere TEST_DATABASE_URL, TEST_REDIS_URL y TEST_ADMIN_DATABASE_URL")

BODY = {"name": "Agente", "instructions": "Atiende llamadas con amabilidad.", "tools": []}


def test_concurrent_agent_creation_respects_the_limit_on_postgres():
    async def main():
        eng = create_async_engine(ADMIN_URL)
        try:
            org = (await pv.create_organization(eng, "Org concurrencia", "conc@example.com"))["org_id"]
        finally:
            await eng.dispose()
        s = Settings(environment="test", jwt_secret=get_settings().jwt_secret, persistence_backend="postgres", state_backend="redis",
                     database_url=PG_URL, redis_url=REDIS_URL, vad_backend="energy", retention_job_enabled=False, plans_enforced=True,
                     plan_limits_json='{"starter": {"max_agents": 2}}', rate_limit_api_per_min=10**6)
        app = create_app(s)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
                h = {"Authorization": f"Bearer {issue_token('u', org, 'admin')}"}
                res = await asyncio.gather(*[c.post("/api/v1/agents", json=BODY, headers=h) for _ in range(12)])
                return [r.status_code for r in res], len((await c.get("/api/v1/agents", headers=h)).json()["items"])

    codes, total = asyncio.run(main())
    assert codes.count(201) == 2 and total == 2, (codes, total)


def test_concurrent_webhooks_and_integrations_respect_their_limits_on_postgres():
    async def main():
        eng = create_async_engine(ADMIN_URL)
        try:
            org = (await pv.create_organization(eng, "Org concurrencia 2", "conc2@example.com"))["org_id"]
        finally:
            await eng.dispose()
        s = Settings(environment="test", jwt_secret=get_settings().jwt_secret, persistence_backend="postgres", state_backend="redis",
                     database_url=PG_URL, redis_url=REDIS_URL, vad_backend="energy", retention_job_enabled=False, webhooks_max_per_org=2,
                     rate_limit_api_per_min=10**6, webhooks_allow_private=True)
        app = create_app(s, webhook_resolver=lambda host: ["93.184.216.34"])
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
                h = {"Authorization": f"Bearer {issue_token('u', org, 'admin')}"}
                hooks = await asyncio.gather(*[c.post("/api/v1/webhooks", json={"url": f"https://h{i}.example.com/x"}, headers=h) for i in range(10)])
                integs = await asyncio.gather(*[c.post("/api/v1/integrations", headers=h, json={"name": f"crm-{i}", "base_url": "https://crm.example.com"})
                                                for i in range(30)])
                return [r.status_code for r in hooks], [r.status_code for r in integs]

    hooks, integs = asyncio.run(main())
    assert hooks.count(201) == 2, hooks
    assert integs.count(201) == 20, integs
