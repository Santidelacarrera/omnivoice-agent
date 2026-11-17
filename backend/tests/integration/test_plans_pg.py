"""Migración 017 contra PostgreSQL real: plan por organización (lo cambia el operador), cuotas aplicadas y consumo del mes."""
import asyncio
import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
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


def _h(org, role="admin"):
    return {"Authorization": f"Bearer {issue_token('u-plan', org, role)}"}


def admin_run(fn):
    async def main():
        eng = create_async_engine(ADMIN_URL)
        try:
            return await fn(eng)
        finally:
            await eng.dispose()

    return asyncio.run(main())


def test_plan_quotas_usage_and_operator_cli_on_postgres():
    org = admin_run(lambda e: pv.create_organization(e, "Org de planes", "plan-owner@example.com"))["org_id"]
    s = Settings(environment="test", jwt_secret=get_settings().jwt_secret, persistence_backend="postgres", state_backend="redis",
                 database_url=PG_URL, redis_url=REDIS_URL, vad_backend="energy", retention_job_enabled=False, plans_enforced=True,
                 plan_limits_json='{"starter": {"max_agents": 1, "minutes_per_month": 1}, "business": {"max_agents": 3}}')
    with TestClient(create_app(s)) as c:
        snap = c.get("/api/v1/plan", headers=_h(org, "viewer")).json()
        assert snap["plan"] == "starter" and snap["enforced"] is True and snap["usage"]["agents"] == 0  # el valor por defecto de la columna

        assert c.post("/api/v1/agents", headers=_h(org), json=BODY).status_code == 201
        over = c.post("/api/v1/agents", headers=_h(org), json=BODY)
        assert over.status_code == 409 and "1 agentes" in over.text

        # el operador sube el plan con el CLI (la aplicación solo lo lee)
        assert admin_run(lambda e: pv.set_plan(e, org, "business")) == {"org_id": org, "plan": "business"}
        assert c.get("/api/v1/plan", headers=_h(org, "viewer")).json()["plan"] == "business"
        assert c.post("/api/v1/agents", headers=_h(org), json=BODY).status_code == 201
        with pytest.raises(pv.ProvisioningError):
            admin_run(lambda e: pv.set_plan(e, org, "gratis"))
        with pytest.raises(pv.ProvisioningError):
            admin_run(lambda e: pv.set_plan(e, "00000000-0000-0000-0000-0000000000dd", "business"))

        # consumo del mes desde usage_records y bloqueo de sesiones nuevas
        assert c.post("/api/v1/sessions", headers=_h(org, "customer"), json={}).status_code == 200
        admin_run(lambda e: _add_usage(e, org, 120))  # 2 minutos > límite de 1 del plan starter
        admin_run(lambda e: pv.set_plan(e, org, "starter"))
        snap = c.get("/api/v1/plan", headers=_h(org, "viewer")).json()
        assert snap["usage"]["minutes_month"] == 2.0 and snap["percent"]["minutes_per_month"] == 200.0
        blocked = c.post("/api/v1/sessions", headers=_h(org, "customer"), json={})
        assert blocked.status_code == 429 and "minutos" in blocked.text


async def _add_usage(eng, org, seconds):
    async with eng.begin() as conn:
        await conn.execute(text("INSERT INTO usage_records (org_id, audio_seconds, estimated_cost_usd) VALUES (:o, :s, 0.12)"), {"o": org, "s": seconds})


def test_app_role_can_read_but_not_change_the_plan():
    async def attempt():
        eng = create_async_engine(PG_URL)
        try:
            async with eng.begin() as conn:
                await conn.execute(text("SELECT set_config('app.org_id', '00000000-0000-0000-0000-000000000001', true)"))
                plan = (await conn.execute(text("SELECT plan FROM organizations WHERE id = '00000000-0000-0000-0000-000000000001'"))).scalar()
            try:
                async with eng.begin() as conn:
                    await conn.execute(text("UPDATE organizations SET plan = 'enterprise' WHERE id = '00000000-0000-0000-0000-000000000001'"))
                return plan, False
            except Exception:  # noqa: BLE001
                return plan, True
        finally:
            await eng.dispose()

    plan, denied = asyncio.run(attempt())
    assert plan in ("starter", "business", "professional", "enterprise") and denied
