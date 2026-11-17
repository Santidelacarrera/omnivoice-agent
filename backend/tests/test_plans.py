"""Etapa 9: planes y cuotas (sin precios): límites, consumo mensual, bloqueo opcional, alertas y margen."""
import asyncio
import json
import re
import uuid
from datetime import datetime, timezone

import pytest

from app.billing.plans import DEFAULT_PLANS, QuotaExceeded, load_plans
from app.security.auth import issue_token
from tests.test_api import make_client
from tests.test_telephony import make as make_telephony

ORG = str(uuid.uuid4())
BODY = {"name": "Agente", "instructions": "Atiende llamadas con amabilidad.", "tools": []}


def h(org=ORG, role="admin"):
    return {"Authorization": f"Bearer {issue_token('u', org, role)}"}


def run(coro):
    return asyncio.run(coro)


def set_plan(c, plan, org=ORG):
    c.app.state.db.org_plans[org] = plan


def use_minutes(c, minutes, org=ORG):
    c.app.state.db.convs[f"conv-{uuid.uuid4()}"] = {"org_id": org, "audio_seconds": minutes * 60}


# ---------- configuración ----------
def test_defaults_have_no_prices_and_enterprise_is_unlimited():
    assert all(p["price_usd_month"] is None for p in DEFAULT_PLANS.values())
    assert all(v is None for k, v in DEFAULT_PLANS["enterprise"].items() if k not in ("name",))
    assert DEFAULT_PLANS["starter"]["minutes_per_month"] < DEFAULT_PLANS["business"]["minutes_per_month"] < DEFAULT_PLANS["professional"]["minutes_per_month"]


def test_overrides_are_applied_and_invalid_ones_fail_at_startup():
    plans = load_plans('{"starter": {"minutes_per_month": 10, "price_usd_month": 49.5, "max_agents": null}}')
    assert plans["starter"]["minutes_per_month"] == 10 and plans["starter"]["price_usd_month"] == 49.5 and plans["starter"]["max_agents"] is None
    assert DEFAULT_PLANS["starter"]["minutes_per_month"] == 300  # los valores por defecto no se mutan
    for bad in ("no json", "[1]", '{"inventado": {}}', '{"starter": {"inventado": 1}}', '{"starter": {"max_agents": -1}}',
                '{"starter": {"max_agents": "mucho"}}', '{"starter": {"max_agents": true}}', '{"starter": 5}'):
        with pytest.raises(ValueError):
            load_plans(bad)


# ---------- consumo y resumen ----------
def test_plan_endpoint_shows_usage_percent_and_margin_when_a_price_is_set():
    c = make_client(plan_limits_json='{"starter": {"minutes_per_month": 100, "price_usd_month": 10}}')
    c.post("/api/v1/agents", json=BODY, headers=h())
    use_minutes(c, 50)
    r = c.get("/api/v1/plan", headers=h("%s" % ORG, "viewer")).json()
    assert r["plan"] == "starter" and r["enforced"] is False
    assert r["limits"]["minutes_per_month"] == 100 and r["usage"]["minutes_month"] == 50.0 and r["usage"]["agents"] == 1
    assert r["percent"]["minutes_per_month"] == 50.0 and r["percent"]["max_agents"] == 50.0
    assert r["margin"]["price_usd"] == 10 and r["margin"]["margin_usd"] == round(10 - r["usage"]["estimated_cost_usd_month"], 2)
    no_price = make_client().get("/api/v1/plan", headers=h()).json()
    assert no_price["margin"] is None  # sin precio no se inventa un margen


def test_plan_endpoint_permissions_and_isolation():
    c = make_client()
    use_minutes(c, 5, ORG)
    use_minutes(c, 99, "otra")
    assert c.get("/api/v1/plan", headers=h(ORG, "customer")).status_code == 403
    assert c.get("/api/v1/plan").status_code == 401
    assert c.get("/api/v1/plan", headers=h(ORG, "viewer")).json()["usage"]["minutes_month"] == 5.0
    assert c.get("/api/v1/plan", headers=h("otra", "viewer")).json()["usage"]["minutes_month"] == 99.0  # cada una ve solo lo suyo


# ---------- sin activar: solo se mide ----------
def test_nothing_is_blocked_when_quotas_are_not_enforced():
    c = make_client(plan_limits_json='{"starter": {"max_agents": 1, "minutes_per_month": 1}}')
    for _ in range(3):
        assert c.post("/api/v1/agents", json=BODY, headers=h()).status_code == 201
    use_minutes(c, 500)
    assert c.post("/api/v1/sessions", json={}, headers=h(ORG, "customer")).status_code == 200


# ---------- activadas: bloqueo ----------
def test_agent_limit_counts_only_live_agents_and_upgrading_lifts_it():
    c = make_client(plans_enforced=True)
    ids = [c.post("/api/v1/agents", json=BODY, headers=h()).json()["id"] for _ in range(2)]
    over = c.post("/api/v1/agents", json=BODY, headers=h())
    assert over.status_code == 409 and "starter" in over.text and "2 agentes" in over.text and "Actualiza el plan" in over.text
    c.post(f"/api/v1/agents/{ids[0]}/archive", headers=h())  # archivar libera cupo
    assert c.post("/api/v1/agents", json=BODY, headers=h()).status_code == 201
    assert c.post("/api/v1/agents", json=BODY, headers=h()).status_code == 409
    set_plan(c, "business")
    assert c.post("/api/v1/agents", json=BODY, headers=h()).status_code == 201
    set_plan(c, "enterprise")
    for _ in range(20):
        assert c.post("/api/v1/agents", json=BODY, headers=h()).status_code == 201  # sin límite
    assert c.post("/api/v1/agents", json=BODY, headers=h("otra")).status_code == 201  # los cupos son por organización


def test_phone_number_and_knowledge_limits():
    c = make_client(plans_enforced=True, plan_limits_json='{"starter": {"max_phone_numbers": 1, "max_kb_sources": 1}}')
    assert c.post("/api/v1/phone-numbers", json={"e164": "+56911110001"}, headers=h()).status_code == 201
    over = c.post("/api/v1/phone-numbers", json={"e164": "+56911110002"}, headers=h())
    assert over.status_code == 409 and "números telefónicos" in over.text
    assert c.post("/api/v1/knowledge/sources", json={"name": "a", "text": "Primer documento con texto suficiente."}, headers=h()).status_code == 201
    over = c.post("/api/v1/knowledge/sources", json={"name": "b", "text": "Segundo documento con otro texto distinto."}, headers=h())
    assert over.status_code == 409 and "documentos de conocimiento" in over.text


def test_monthly_minutes_block_new_sessions_until_the_plan_changes():
    c = make_client(plans_enforced=True, plan_limits_json='{"starter": {"minutes_per_month": 60}}')
    assert c.post("/api/v1/sessions", json={}, headers=h(ORG, "customer")).status_code == 200
    use_minutes(c, 61)
    r = c.post("/api/v1/sessions", json={}, headers=h(ORG, "customer"))
    assert r.status_code == 429 and "minutos de conversación al mes" in r.text
    assert c.post("/api/v1/sessions", json={}, headers=h("otra", "customer")).status_code == 200  # otra organización no se ve afectada
    set_plan(c, "business")
    assert c.post("/api/v1/sessions", json={}, headers=h(ORG, "customer")).status_code == 200


def test_ws_connection_is_refused_if_the_quota_ran_out_after_creating_the_session():
    c = make_client(plans_enforced=True, plan_limits_json='{"starter": {"minutes_per_month": 60}}')
    sid = c.post("/api/v1/sessions", json={}, headers=h(ORG, "customer")).json()["session_id"]
    ticket = c.post(f"/api/v1/sessions/{sid}/connect", headers=h(ORG, "customer")).json()["ticket"]
    use_minutes(c, 61)
    with pytest.raises(Exception):
        with c.websocket_connect(f"/ws/audio?ticket={ticket}") as ws:
            ws.receive()


def test_inbound_call_is_politely_rejected_when_minutes_are_exhausted():
    from tests.test_telephony import signed_post

    c, tw = make_telephony(plans_enforced=True, plan_limits_json='{"starter": {"minutes_per_month": 10}}')
    org = "00000000-0000-0000-0000-000000000001"
    use_minutes(c, 11, org)
    r = signed_post(c, {"CallSid": "CA1", "From": "+34600000001", "To": "+34910000000"})
    assert "<Stream" not in r.text and "<Hangup/>" in r.text
    assert any(e["action"] == "telephony.call_rejected" and e["detail"]["reason"] == "plan_minutes_exhausted" for e in c.app.state.db.audits)


def test_outbound_daily_limit():
    c, tw = make_telephony(plans_enforced=True, plan_limits_json='{"starter": {"max_outbound_per_day": 1}}',
                           outbound_window_start="00:00", outbound_window_end="23:59", outbound_days="0,1,2,3,4,5,6")
    org = "00000000-0000-0000-0000-000000000001"
    aid = c.post("/api/v1/agents", json=BODY, headers=h(org)).json()["id"]
    c.post(f"/api/v1/agents/{aid}/publish", headers=h(org))
    c.post("/api/v1/phone-numbers", json={"e164": "+56911110000", "agent_id": aid}, headers=h(org))
    first = c.post("/api/v1/outbound-calls", json={"to": "+56987650001", "consent_basis": "existing_customer"}, headers=h(org))
    assert first.status_code == 202
    second = c.post("/api/v1/outbound-calls", json={"to": "+56987650002", "consent_basis": "existing_customer"}, headers=h(org))
    assert second.status_code == 429 and "llamadas salientes al día" in second.text


# ---------- alertas ----------
def test_usage_alerts_fire_once_per_month_and_threshold_and_reach_webhooks():
    c = make_client(plan_limits_json='{"starter": {"minutes_per_month": 100}}')
    c.app.state.webhooks.resolver = lambda host: ["93.184.216.34"]
    assert c.post("/api/v1/webhooks", json={"url": "https://hooks.example.com/x", "events": ["usage.alert"]}, headers=h()).status_code == 201
    plans = c.app.state.plans
    assert run(plans.maybe_alert(ORG)) == []  # sin consumo no hay alerta
    use_minutes(c, 85)
    assert run(plans.maybe_alert(ORG)) == [80]
    assert run(plans.maybe_alert(ORG)) == []  # ya avisado este mes
    use_minutes(c, 20)
    assert run(plans.maybe_alert(ORG)) == [100]
    assert run(plans.maybe_alert(ORG)) == []
    payloads = [d["payload"] for d in c.app.state.db.webhook_deliveries]
    assert [p["data"]["threshold_percent"] for p in payloads] == [80, 100] and all(p["type"] == "usage.alert" for p in payloads)
    assert payloads[0]["data"] == {"metric": "minutes_per_month", "threshold_percent": 80, "minutes_used": 85.0, "minutes_limit": 100, "plan": "starter"}
    assert not re.search(r"\+\d{6,}", json.dumps(payloads))  # sin números de teléfono
    next_month = datetime(2031, 3, 1, tzinfo=timezone.utc)
    assert run(plans.maybe_alert(ORG, now=next_month)) == [80, 100]  # al mes siguiente se vuelve a avisar


def test_enterprise_has_no_alerts_and_alert_errors_never_propagate():
    c = make_client()
    set_plan(c, "enterprise")
    use_minutes(c, 99999)
    assert run(c.app.state.plans.maybe_alert(ORG)) == []

    class Boom:
        async def org_plan(self, org):
            raise RuntimeError("base caída")

    c.app.state.plans._db = lambda: Boom()
    assert run(c.app.state.plans.maybe_alert(ORG)) == []  # nunca rompe el cierre de una llamada


def test_quota_exceeded_message_is_user_facing():
    e = QuotaExceeded("max_agents", 2, "starter")
    assert "2 agentes" in e.message and "starter" in e.message
