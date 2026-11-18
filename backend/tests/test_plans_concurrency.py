"""Cuotas del plan bajo concurrencia: comprobar el límite y crear debe ser atómico (en este proceso)."""
import asyncio
import uuid

import httpx

from app.core.config import Settings
from app.main import create_app
from app.security.auth import issue_token

ORG = str(uuid.uuid4())
BODY = {"name": "Agente", "instructions": "Atiende llamadas con amabilidad.", "tools": []}


def test_concurrent_creates_never_exceed_the_plan_limit():
    s = Settings(environment="test", jwt_secret="t" * 40, vad_backend="energy", plans_enforced=True,
                 plan_limits_json='{"starter": {"max_agents": 2}}', rate_limit_api_per_min=10**6)
    app = create_app(s)

    async def run():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
                h = {"Authorization": f"Bearer {issue_token('u', ORG, 'admin')}"}
                res = await asyncio.gather(*[c.post("/api/v1/agents", json=BODY, headers=h) for _ in range(12)])
                return [r.status_code for r in res], len((await c.get("/api/v1/agents", headers=h)).json()["items"])

    codes, total = asyncio.run(run())
    assert codes.count(201) == 2 and codes.count(409) == 10 and total == 2, (codes, total)
