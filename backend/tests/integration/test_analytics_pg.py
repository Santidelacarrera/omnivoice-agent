"""Etapa 9/10 contra PostgreSQL real: agregados SQL de /analytics y /usage (por diferencia: la base puede tener otras llamadas)."""
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


def _h(org, role="analyst"):
    return {"Authorization": f"Bearer {issue_token('u-an', org, role)}"}


def test_analytics_aggregates_in_sql_and_isolates_orgs():
    s = Settings(environment="test", jwt_secret=get_settings().jwt_secret, persistence_backend="postgres", state_backend="redis",
                 database_url=PG_URL, redis_url=REDIS_URL, vad_backend="energy", retention_job_enabled=False)
    with TestClient(create_app(s)) as c:
        db = c.app.state.db
        before = c.get("/api/v1/analytics?days=1", headers=_h(DEMO)).json()

        def call(direction, final, answered):
            sid = "CA" + uuid.uuid4().hex
            assert c.portal.call(db.upsert_call, DEMO, "twilio", sid, direction, None, "+34910000000", "hash")
            for i, st in enumerate((["ANSWERED"] if answered else []) + [final]):
                c.portal.call(db.apply_call_event, DEMO, "twilio", sid, st, f"k{i}", {})

        call("inbound", "COMPLETED", True)
        call("inbound", "TRANSFERRED", True)
        call("outbound", "NO_ANSWER", False)
        call("outbound", "BUSY", False)

        after = c.get("/api/v1/analytics?days=1", headers=_h(DEMO)).json()
        delta = {k: after["calls"][k] - before["calls"][k] for k in ("total", "inbound", "outbound", "answered", "missed", "transferred")}
        assert delta == {"total": 4, "inbound": 2, "outbound": 2, "answered": 2, "missed": 2, "transferred": 1}
        assert after["calls"]["avg_duration_s"] is not None
        for st, n in {"COMPLETED": 1, "TRANSFERRED": 1, "NO_ANSWER": 1, "BUSY": 1}.items():
            assert after["by_status"].get(st, 0) - before["by_status"].get(st, 0) == n
        assert sum(d["calls"] for d in after["by_day"]) == after["calls"]["total"]
        assert after["rates"]["answer_rate"] is not None

        usage = c.get("/api/v1/usage?days=1", headers=_h(DEMO)).json()
        assert usage["calls"] == after["calls"]["total"] and usage["period_days"] == 1 and "note" in usage

        # otra organización no ve nada de esto
        assert c.get("/api/v1/analytics?days=1", headers=_h(OTHER)).json()["calls"]["total"] == 0
        assert c.get("/api/v1/analytics", headers=_h(DEMO, "customer")).status_code == 403
