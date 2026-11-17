"""Etapa 9/10: GET /usage y GET /analytics (métricas comerciales por organización)."""
import time
import uuid

import pytest

from app.security.auth import issue_token
from app.telephony.call_state import TERMINAL
from tests.test_api import make_client

ORG, OTHER = str(uuid.uuid4()), str(uuid.uuid4())


def h(org=ORG, role="analyst"):
    return {"Authorization": f"Bearer {issue_token('u', org, role)}"}


def seed(c, org, sid, direction="inbound", status="COMPLETED", answered=True, duration=60.0, agent=None, age_days=0.0):
    """Inserta una llamada directamente en la persistencia en memoria."""
    now = time.time() - age_days * 86400
    cid = str(uuid.uuid4())
    c.app.state.db.calls[cid] = {"id": cid, "org_id": org, "provider": "twilio", "sid": sid, "direction": direction, "agent_id": agent,
                                 "our_number": None, "peer_hash": None, "status": status, "created_at": now,
                                 "answered_at": now + 5 if answered else None,
                                 "ended_at": (now + 5 + duration) if status in TERMINAL else None}  # en curso: aún sin fin


def test_empty_org_has_zero_metrics_and_null_rates():
    c = make_client()
    a = c.get("/api/v1/analytics", headers=h()).json()
    assert a["period_days"] == 30 and a["calls"]["total"] == 0 and a["by_day"] == [] and a["by_agent"] == []
    assert a["rates"] == {"answer_rate": None, "missed_rate": None, "transfer_rate": None} and a["calls"]["avg_duration_s"] is None
    u = c.get("/api/v1/usage", headers=h()).json()
    assert u["calls"] == 0 and u["cost_per_call_usd"] is None and u["audio_minutes"] == 0


def test_metrics_are_computed_from_calls():
    c = make_client()
    aid = str(uuid.uuid4())
    seed(c, ORG, "1", "inbound", "COMPLETED", True, 60, aid)
    seed(c, ORG, "2", "inbound", "TRANSFERRED", True, 120, aid)
    seed(c, ORG, "3", "outbound", "NO_ANSWER", False, 0)
    seed(c, ORG, "4", "outbound", "BUSY", False, 0)
    seed(c, ORG, "5", "inbound", "CONNECTED", True, 0, aid)  # en curso: sin fin
    a = c.get("/api/v1/analytics", headers=h()).json()
    calls = a["calls"]
    assert calls["total"] == 5 and calls["inbound"] == 3 and calls["outbound"] == 2
    assert calls["answered"] == 3 and calls["missed"] == 2 and calls["transferred"] == 1
    assert calls["avg_duration_s"] == 90.0  # solo cuentan las ya terminadas y atendidas
    assert a["by_status"] == {"COMPLETED": 1, "TRANSFERRED": 1, "NO_ANSWER": 1, "BUSY": 1, "CONNECTED": 1}
    assert a["rates"] == {"answer_rate": 0.6, "missed_rate": 0.4, "transfer_rate": 0.3333}
    assert a["by_agent"] == [{"agent_id": aid, "calls": 3, "answered": 3}]
    assert len(a["by_day"]) == 1 and a["by_day"][0]["calls"] == 5 and a["by_day"][0]["missed"] == 2


def test_period_filter_and_bounds():
    c = make_client()
    seed(c, ORG, "new", age_days=1)
    seed(c, ORG, "old", age_days=40)
    assert c.get("/api/v1/analytics?days=7", headers=h()).json()["calls"]["total"] == 1
    assert c.get("/api/v1/analytics?days=90", headers=h()).json()["calls"]["total"] == 2
    assert c.get("/api/v1/analytics?days=0", headers=h()).json()["period_days"] == 1  # acotado
    assert c.get("/api/v1/analytics?days=99999", headers=h()).json()["period_days"] == 365
    days = [d["date"] for d in c.get("/api/v1/analytics?days=90", headers=h()).json()["by_day"]]
    assert days == sorted(days) and len(days) == 2


def test_isolation_and_permissions():
    c = make_client()
    seed(c, ORG, "mine")
    seed(c, OTHER, "theirs")
    assert c.get("/api/v1/analytics", headers=h(OTHER)).json()["calls"]["total"] == 1
    assert c.get("/api/v1/analytics", headers=h(str(uuid.uuid4()))).json()["calls"]["total"] == 0
    for role, code in (("viewer", 200), ("analyst", 200), ("operator", 200), ("customer", 403)):
        assert c.get("/api/v1/analytics", headers=h(ORG, role)).status_code == code
        assert c.get("/api/v1/usage", headers=h(ORG, role)).status_code == code
    assert c.get("/api/v1/usage").status_code == 401


@pytest.mark.parametrize("path", ["/api/v1/usage", "/api/v1/analytics"])
def test_responses_expose_no_personal_data(path):
    c = make_client()
    seed(c, ORG, "CAsecreto123")
    body = c.get(path, headers=h()).text
    assert "CAsecreto123" not in body and "peer_hash" not in body and "+" not in body.replace("+00:00", "")


def test_usage_cost_per_call():
    c = make_client()
    seed(c, ORG, "1")
    seed(c, ORG, "2")
    c.app.state.db.convs["x"] = {"org_id": ORG, "audio_seconds": 120}
    u = c.get("/api/v1/usage", headers=h()).json()
    assert u["audio_minutes"] == 2.0 and u["calls"] == 2 and u["cost_per_call_usd"] == round(u["estimated_cost_usd"] / 2, 4)
