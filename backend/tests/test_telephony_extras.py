"""Telefonía: destinos de transferencia permitidos, vigilante de llamadas abandonadas y reglas de alerta."""
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from app.security.auth import issue_token
from app.telephony import call_state as cs
from app.telephony.destinations import is_allowed, parse_prefixes
from tests.test_call_tracking import status_post
from tests.test_telephony import make

ORG = str(uuid.uuid4())
NUM = "+56911110000"
BODY = {"name": "Recepción", "instructions": "Atiende llamadas de la clínica con amabilidad.", "tools": []}
ROOT = Path(__file__).resolve().parents[2]


def h(org=ORG, role="admin"):
    return {"Authorization": f"Bearer {issue_token('u', org, role)}"}


def setup(c):
    aid = c.post("/api/v1/agents", json=BODY, headers=h()).json()["id"]
    c.post(f"/api/v1/agents/{aid}/publish", headers=h())
    return c.post("/api/v1/phone-numbers", json={"e164": NUM, "agent_id": aid}, headers=h()).json()["id"]


# ---------- destinos permitidos ----------
@pytest.mark.parametrize("number,allowed,blocked,ok", [
    ("+56922223333", ("+56", "+34"), (), True), ("+34911111111", ("+56", "+34"), (), True), ("+19005551234", ("+56", "+34"), (), False),
    ("+56922223333", (), (), True),  # sin límites
    ("+56600123456", ("+56",), ("+56600",), False),  # bloqueado aunque el prefijo esté permitido
    ("56922223333", (), (), False), ("", (), (), False)])
def test_is_allowed(number, allowed, blocked, ok):
    assert is_allowed(number, allowed, blocked) is ok


def test_parse_prefixes():
    assert parse_prefixes(" +56 , +34,,") == ("+56", "+34") and parse_prefixes("") == ()


def test_transfer_target_is_validated_when_saved():
    c, _ = make(transfer_allowed_prefixes="+56,+34", transfer_blocked_prefixes="+56600")
    pid = setup(c)
    bad = c.patch(f"/api/v1/phone-numbers/{pid}", json={"transfer_number": "+19005551234"}, headers=h())
    assert bad.status_code == 422 and "no está permitido" in bad.text
    assert c.patch(f"/api/v1/phone-numbers/{pid}", json={"transfer_number": "+56600123456"}, headers=h()).status_code == 422
    assert c.patch(f"/api/v1/phone-numbers/{pid}", json={"transfer_number": "+56922223333"}, headers=h()).status_code == 200
    assert c.patch(f"/api/v1/phone-numbers/{pid}", json={"transfer_number": None}, headers=h()).status_code == 200  # quitarlo siempre vale
    assert c.post("/api/v1/phone-numbers", json={"e164": "+56933334444", "transfer_number": "+19005551234"}, headers=h()).status_code == 422


def test_transfer_is_blocked_if_configuration_changed_after_saving():
    """Un número guardado antes de endurecer la política no se marca: el servidor lo vuelve a comprobar al transferir."""
    c, tw = make(transfer_allowed_prefixes="+56")
    pid = setup(c)
    c.patch(f"/api/v1/phone-numbers/{pid}", json={"transfer_number": "+56922223333"}, headers=h())
    sid = "CAtr"
    cid = str(uuid.uuid4())
    c.app.state.db.calls[cid] = {"id": cid, "org_id": ORG, "provider": "twilio", "sid": sid, "direction": "inbound", "agent_id": None,
                                 "our_number": NUM, "peer_hash": "h", "status": "CONNECTED", "created_at": 1.0, "answered_at": 1.0, "ended_at": None}
    c.__enter__()
    # política endurecida después: ya no se admite el prefijo del operador
    c.app.state.dialer.s.transfer_allowed_prefixes = "+34"
    r = c.post(f"/api/v1/calls/{cid}/transfer", json={}, headers=h())
    assert r.status_code == 409 and not tw.redirects  # destino bloqueado: no hay transferencia
    c.app.state.dialer.s.transfer_allowed_prefixes = "+56"


# ---------- llamadas abandonadas ----------
def seed_call(c, sid, status, age_s, org=ORG):
    db = c.app.state.db
    cid = str(uuid.uuid4())
    ts = datetime.now(timezone.utc).timestamp() - age_s
    db.calls[cid] = {"id": cid, "org_id": org, "provider": "twilio", "sid": sid, "direction": "inbound", "agent_id": None, "our_number": NUM,
                     "peer_hash": "h", "status": status, "created_at": ts, "answered_at": ts if status in ("ANSWERED", "CONNECTED") else None,
                     "ended_at": None}
    db.call_events.append({"call_id": cid, "org_id": org, "status": status, "event_key": "created", "applied": True, "detail": {}, "ts": ts})


def test_reaper_closes_only_stale_unfinished_calls_and_hangs_up_live_ones():
    c, tw = make()
    seed_call(c, "CAold-ringing", "RINGING", 1000)  # > 900 s sin contestar
    seed_call(c, "CAnew-ringing", "RINGING", 100)  # reciente
    seed_call(c, "CAold-live", "CONNECTED", 11000)  # > 3 h
    seed_call(c, "CAlive-ok", "CONNECTED", 600)  # conversación normal
    seed_call(c, "CAdone", "COMPLETED", 99999)  # ya terminada: no se toca
    dialer = c.app.state.dialer
    assert asyncio.run(dialer.reap()) == 2
    states = {x["sid"]: x["status"] for x in c.app.state.db.calls.values()}
    assert states == {"CAold-ringing": "FAILED", "CAnew-ringing": "RINGING", "CAold-live": "FAILED", "CAlive-ok": "CONNECTED", "CAdone": "COMPLETED"}
    assert tw.hangups == [("CAold-live", False)]  # solo se cuelga la que podía seguir viva en el proveedor
    ev = [e for e in c.app.state.db.call_events if e["event_key"] == "abandoned"]
    assert len(ev) == 2 and all(e["detail"]["reason"] == "abandoned" for e in ev)
    assert asyncio.run(dialer.reap()) == 0  # idempotente


def test_reaped_call_drives_the_outbound_retry_and_the_webhook_event():
    c, tw = make(outbound_window_start="00:00", outbound_window_end="23:59", outbound_days="0,1,2,3,4,5,6")
    setup(c)
    job = c.post("/api/v1/outbound-calls", json={"to": "+56987654321", "consent_basis": "existing_customer"}, headers=h()).json()
    assert job["status"] == "DIALING"
    for call in c.app.state.db.calls.values():  # el proveedor nunca avisó
        call["created_at"] -= 5000
        for e in c.app.state.db.call_events:
            e["ts"] -= 5000
    assert asyncio.run(c.app.state.dialer.reap()) == 1
    j = c.get(f"/api/v1/outbound-calls/{job['id']}", headers=h()).json()
    assert j["status"] == "SCHEDULED" and j["last_result"] == "FAILED" and j["attempts"] == 1  # se reintentará
    assert c.get("/api/v1/calls", headers=h()).json()["items"][0]["status"] == cs.FAILED
    assert status_post(c, {"CallSid": "CAout", "CallStatus": "completed", "SequenceNumber": "9"}).status_code == 204  # un aviso tardío no revive


import asyncio  # noqa: E402


# ---------- reglas de alerta ----------
def load_rules():
    doc = yaml.safe_load((ROOT / "infrastructure" / "alerts.yml").read_text(encoding="utf-8"))
    return [r for g in doc["groups"] for r in g["rules"]]


def test_alert_rules_are_well_formed():
    rules = load_rules()
    names = [r["alert"] for r in rules]
    assert len(names) == len(set(names))  # sin duplicados
    for r in rules:
        assert r["expr"] and r["labels"]["severity"] in ("warning", "critical") and r["annotations"]["summary"], r["alert"]
        assert r["expr"].count("(") == r["expr"].count(")") and r["expr"].count("{") == r["expr"].count("}"), r["alert"]
    for needed in ("ProveedorDeTelefoniaFallando", "LlamadasAbandonadas", "WebhooksSinEntregar", "WebhookDesactivadoAutomaticamente",
                   "DestinoDeTransferenciaBloqueado", "FirmasDeTelefoniaInvalidas", "MarcacionesSalientesFallidas"):
        assert needed in names


def test_alert_rules_only_reference_metrics_and_labels_that_exist_in_code():
    code = "\n".join(p.read_text(encoding="utf-8") for p in (ROOT / "backend" / "app").rglob("*.py"))
    declared = set(re.findall(r'(?:Counter|Histogram|Gauge)\(\s*"(omnivoice_[a-z_]+?)(?:_total)?"', code))
    for r in load_rules():
        for metric in re.findall(r"\b(omnivoice_[a-z_]+)", r["expr"]):
            base = re.sub(r"_(bucket|total|count|sum)$", "", metric)
            assert base in declared or metric in declared, f"{r['alert']}: la métrica {metric} no existe en el código"
        for kind in re.findall(r'kind(?:=~?)"([^"]+)"', r["expr"]):
            for k in kind.split("|"):
                assert f'"{k}"' in code, f"{r['alert']}: kind {k} no se emite en el código"
        for key in re.findall(r'(?:event|outcome)="([a-z_]+)"', r["expr"]):
            assert f'"{key}"' in code, f"{r['alert']}: etiqueta {key} no se emite en el código"
