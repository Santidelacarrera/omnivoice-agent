"""Etapa 3: llamadas salientes controladas: consentimiento, horario, duplicados, lista de exclusión, reintentos y privacidad."""
import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.security.auth import issue_token
from app.telephony import outbound as ob
from tests.test_call_tracking import status_post
from tests.test_telephony import make

SCL = ZoneInfo("America/Santiago")
ORG, OTHER = str(uuid.uuid4()), str(uuid.uuid4())
NUM, TO = "+56911110000", "+56987654321"
BODY = {"name": "Recepción", "instructions": "Atiende llamadas de la clínica con amabilidad.", "tools": []}
POLICY = ob.OutboundPolicy(start=ob.parse_hhmm("09:00"), end=ob.parse_hhmm("20:00"), days=ob.parse_days("0,1,2,3,4,5"),
                           timezone="America/Santiago", max_attempts=3)


def h(org=ORG, role="admin"):
    return {"Authorization": f"Bearer {issue_token('u', org, role)}"}


def setup(c, org=ORG, number=NUM):
    aid = c.post("/api/v1/agents", json=BODY, headers=h(org)).json()["id"]
    c.post(f"/api/v1/agents/{aid}/publish", headers=h(org))
    assert c.post("/api/v1/phone-numbers", json={"e164": number, "agent_id": aid}, headers=h(org)).status_code == 201


def body(**kw):
    return {"to": TO, "consent_basis": "existing_customer", **kw}


def closed_today_days() -> str:
    """Días permitidos = todos menos hoy (en Santiago): hoy la ventana está cerrada pase lo que pase con el reloj."""
    return ",".join(str(d) for d in range(7) if d != datetime.now(SCL).weekday())


def app_with_window(**over):
    return make(**{"outbound_window_start": "09:00", "outbound_window_end": "20:00", "outbound_days": "0,1,2,3,4,5", **over})


def run(coro):
    return asyncio.run(coro)


# ---------- política pura ----------
@pytest.mark.parametrize("local,ok", [
    (datetime(2030, 1, 7, 9, 0, tzinfo=SCL), True), (datetime(2030, 1, 7, 8, 59, tzinfo=SCL), False),
    (datetime(2030, 1, 7, 19, 59, tzinfo=SCL), True), (datetime(2030, 1, 7, 20, 0, tzinfo=SCL), False),
    (datetime(2030, 1, 6, 12, 0, tzinfo=SCL), False),  # domingo
])
def test_window(local, ok):
    assert ob.in_window(local.astimezone(timezone.utc), POLICY) is ok


def test_next_window_start_skips_closed_hours_and_days():
    sat_night = datetime(2030, 1, 5, 21, 0, tzinfo=SCL).astimezone(timezone.utc)
    assert ob.next_window_start(sat_night, POLICY).astimezone(SCL) == datetime(2030, 1, 7, 9, 0, tzinfo=SCL)  # lunes 09:00
    early = datetime(2030, 1, 7, 6, 0, tzinfo=SCL).astimezone(timezone.utc)
    assert ob.next_window_start(early, POLICY).astimezone(SCL) == datetime(2030, 1, 7, 9, 0, tzinfo=SCL)
    inside = datetime(2030, 1, 7, 10, 0, tzinfo=SCL).astimezone(timezone.utc)
    assert ob.next_window_start(inside, POLICY) == inside


def test_window_uses_the_recipients_timezone():
    t = datetime(2030, 1, 7, 12, 0, tzinfo=timezone.utc)  # 09:00 en Santiago (UTC-3 en enero), 13:00 en Madrid
    assert ob.in_window(t, POLICY) and ob.in_window(t, POLICY, "Europe/Madrid")
    late = datetime(2030, 1, 7, 22, 0, tzinfo=timezone.utc)  # 19:00 Santiago pero 23:00 Madrid
    assert ob.in_window(late, POLICY) and not ob.in_window(late, POLICY, "Europe/Madrid")


def test_retry_delay_grows_and_is_capped():
    assert [ob.retry_delay(POLICY, n).total_seconds() / 60 for n in (1, 2, 3, 20)] == [15, 30, 45, 120]


def test_number_hash_is_salted_per_org():
    assert ob.number_hash(ORG, TO) == ob.number_hash(ORG, TO) and ob.number_hash(ORG, TO) != ob.number_hash(OTHER, TO)
    assert TO not in ob.number_hash(ORG, TO)


@pytest.mark.parametrize("fn,args", [(ob.parse_hhmm, ("25:99",)), (ob.parse_days, ("7",)), (ob.parse_days, ("",)), (ob.zone, ("Marte/Fobos",)),
                                     (ob.validate_consent, ("nada", None)), (ob.validate_consent, ("explicit_opt_in", " "))])
def test_policy_rejects_bad_input(fn, args):
    with pytest.raises(ob.OutboundPolicyError):
        fn(*args)


# ---------- API: consentimiento y permisos ----------
def test_consent_is_mandatory():
    c, tw = app_with_window()
    setup(c)
    assert c.post("/api/v1/outbound-calls", json={"to": TO}, headers=h()).status_code == 422
    assert c.post("/api/v1/outbound-calls", json=body(consent_basis="porque_si"), headers=h()).status_code == 422
    r = c.post("/api/v1/outbound-calls", json=body(consent_basis="explicit_opt_in"), headers=h())
    assert r.status_code == 422 and "consent_ref" in r.text
    assert not tw.calls


def test_roles():
    c, _ = app_with_window(outbound_days=closed_today_days())
    setup(c)
    for role, code in (("viewer", 403), ("analyst", 403), ("operator", 403), ("customer", 403), ("manager", 202), ("owner", 202)):
        r = c.post("/api/v1/outbound-calls", json=body(to=f"+5698765{abs(hash(role)) % 10000:04d}"), headers=h(ORG, role))
        assert r.status_code == code, (role, r.text)
    assert c.get("/api/v1/outbound-calls", headers=h(ORG, "viewer")).status_code == 200


def test_needs_a_number_with_a_published_agent():
    c, _ = app_with_window()
    r = c.post("/api/v1/outbound-calls", json=body(), headers=h())
    assert r.status_code == 422 and "número" in r.text
    assert c.post("/api/v1/outbound-calls", json=body(from_number="+56900000000"), headers=h()).status_code == 422


# ---------- horario ----------
def test_dials_now_inside_window_and_hides_the_number():
    c, tw = make(outbound_window_start="00:00", outbound_window_end="23:59", outbound_days="0,1,2,3,4,5,6")
    setup(c)
    r = c.post("/api/v1/outbound-calls", json=body(), headers=h())
    job = r.json()
    assert r.status_code == 202 and job["status"] == "DIALING" and job["attempts"] == 1 and job["last_call_sid"] == "CAout"
    assert tw.calls[0][:2] == (TO, NUM)
    assert tw.calls[0][2].endswith("/telephony/voice?direction=outbound")
    assert job["to_masked"].startswith("+56") and TO not in json.dumps(job)
    assert TO not in json.dumps(c.get("/api/v1/outbound-calls", headers=h()).json())
    call = c.get("/api/v1/calls", headers=h()).json()["items"][0]
    assert call["direction"] == "outbound" and call["status"] == "QUEUED" and call["our_number"] == NUM


def test_outside_window_is_scheduled_not_dialed():
    c, tw = app_with_window(outbound_days=closed_today_days())
    setup(c)
    r = c.post("/api/v1/outbound-calls", json=body(), headers=h())
    job = r.json()
    assert job["status"] == "SCHEDULED" and not tw.calls
    nxt = datetime.fromisoformat(job["next_attempt_at"]).astimezone(SCL)
    assert nxt > datetime.now(SCL) and nxt.hour == 9 and nxt.weekday() != datetime.now(SCL).weekday()


def test_scheduled_time_outside_hours_moves_to_next_window_and_rejects_bad_input():
    c, tw = app_with_window()
    setup(c)
    far = (datetime.now(timezone.utc) + timedelta(days=3)).replace(hour=2, minute=0)  # 02:00 UTC = 23:00 Santiago
    job = c.post("/api/v1/outbound-calls", json=body(scheduled_at=far.isoformat()), headers=h()).json()
    assert ob.in_window(datetime.fromisoformat(job["next_attempt_at"]), POLICY) and not tw.calls
    assert c.post("/api/v1/outbound-calls", json=body(to="+56911112222", scheduled_at="2030-01-01T10:00:00"), headers=h()).status_code == 422
    assert c.post("/api/v1/outbound-calls", json=body(to="+56911113333", scheduled_at=(datetime.now(timezone.utc) + timedelta(days=60)).isoformat()),
                  headers=h()).status_code == 422
    assert c.post("/api/v1/outbound-calls", json=body(to="+56911114444", timezone="Marte/Fobos"), headers=h()).status_code == 422


def test_tick_defers_when_window_closed_without_spending_an_attempt():
    c, tw = app_with_window()
    setup(c)
    dialer = c.app.state.dialer
    t_ok = datetime(2030, 1, 5, 19, 59, tzinfo=SCL).astimezone(timezone.utc)  # sábado, aún dentro
    job = run(dialer.submit(ORG, "u", to=TO, consent_basis="existing_customer", now=t_ok))["job"]
    assert job["status"] == "SCHEDULED"
    assert run(dialer.tick(datetime(2030, 1, 5, 20, 1, tzinfo=SCL).astimezone(timezone.utc))) == 1  # ya cerró
    j = run(c.app.state.db.get_outbound_job(ORG, job["id"]))
    assert j["status"] == "SCHEDULED" and j["attempts"] == 0 and not tw.calls
    assert j["next_attempt_at"].astimezone(SCL) == datetime(2030, 1, 7, 9, 0, tzinfo=SCL)


# ---------- duplicados, idempotencia, cancelación ----------
def test_duplicates_idempotency_and_cancel():
    c, tw = app_with_window(outbound_days=closed_today_days())
    setup(c)
    first = c.post("/api/v1/outbound-calls", json=body(idempotency_key="clave-0001"), headers=h())
    assert first.status_code == 202 and first.json()["replayed"] is False
    again = c.post("/api/v1/outbound-calls", json=body(idempotency_key="clave-0001"), headers=h()).json()
    assert again["id"] == first.json()["id"] and again["replayed"] is True
    assert c.post("/api/v1/outbound-calls", json=body(), headers=h()).status_code == 409  # mismo número, trabajo activo
    cancelled = c.post(f"/api/v1/outbound-calls/{first.json()['id']}/cancel", headers=h())
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "CANCELLED" and cancelled.json()["to_masked"] is None
    assert c.post(f"/api/v1/outbound-calls/{first.json()['id']}/cancel", headers=h()).status_code == 200  # idempotente
    audit = [e["action"] for e in c.get("/api/v1/audit-logs", headers=h()).json()["items"]]
    assert audit.count("outbound.cancelled") == 1
    assert c.post("/api/v1/outbound-calls", json=body(), headers=h()).status_code == 202  # ya se puede volver a programar
    assert not tw.calls


# ---------- lista de exclusión ----------
def test_do_not_call_blocks_cancels_and_never_stores_the_number():
    c, tw = app_with_window(outbound_days=closed_today_days())
    setup(c)
    job = c.post("/api/v1/outbound-calls", json=body(), headers=h()).json()
    entry = c.post("/api/v1/do-not-call", json={"number": TO, "reason": "pidió no ser llamado"}, headers=h())
    assert entry.status_code == 201 and TO not in entry.text
    assert c.get(f"/api/v1/outbound-calls/{job['id']}", headers=h()).json()["status"] == "CANCELLED"  # lo programado se cancela
    r = c.post("/api/v1/outbound-calls", json=body(), headers=h())
    assert r.status_code == 403 and "exclusión" in r.text and not tw.calls
    listing = c.get("/api/v1/do-not-call", headers=h(ORG, "viewer"))
    assert listing.status_code == 200 and TO not in listing.text and len(listing.json()["items"]) == 1
    assert c.post("/api/v1/do-not-call", json={"number": TO}, headers=h(ORG, "operator")).status_code == 403
    assert c.delete(f"/api/v1/do-not-call/{entry.json()['id']}", headers=h()).status_code == 204
    assert c.post("/api/v1/outbound-calls", json=body(), headers=h()).status_code == 202


def test_do_not_call_added_while_dialing_is_honoured_at_dial_time():
    c, tw = app_with_window()
    setup(c)
    dialer, db = c.app.state.dialer, c.app.state.db
    t = datetime(2030, 1, 7, 10, 0, tzinfo=SCL).astimezone(timezone.utc)
    job = run(dialer.submit(ORG, "u", to=TO, consent_basis="existing_customer", now=t))["job"]
    claimed = run(db.claim_due_outbound_jobs(t, 5, 600))  # el planificador ya lo tomó…
    assert [j["id"] for j in claimed] == [job["id"]]
    run(db.add_dnc(ORG, ob.number_hash(ORG, TO), None, "u"))  # …y justo entonces el cliente pide no ser llamado
    run(dialer._dial(claimed[0], t))
    j = run(db.get_outbound_job(ORG, job["id"]))
    assert not tw.calls and j["status"] == "CANCELLED" and j["to_number"] is None


# ---------- aislamiento ----------
def test_isolation_between_orgs():
    c, _ = app_with_window(outbound_days=closed_today_days())
    setup(c)
    job = c.post("/api/v1/outbound-calls", json=body(), headers=h()).json()
    d = c.post("/api/v1/do-not-call", json={"number": "+56955550000"}, headers=h()).json()
    assert c.get("/api/v1/outbound-calls", headers=h(OTHER)).json()["items"] == []
    assert c.get(f"/api/v1/outbound-calls/{job['id']}", headers=h(OTHER)).status_code == 404
    assert c.post(f"/api/v1/outbound-calls/{job['id']}/cancel", headers=h(OTHER)).status_code == 404
    assert c.get("/api/v1/do-not-call", headers=h(OTHER)).json()["items"] == []
    assert c.delete(f"/api/v1/do-not-call/{d['id']}", headers=h(OTHER)).status_code == 404
    assert c.get("/api/v1/outbound-calls/no-uuid", headers=h()).status_code == 404


# ---------- reintentos ----------
def test_retries_then_exhausts_and_erases_the_number():
    c, tw = make(outbound_window_start="00:00", outbound_window_end="23:59", outbound_days="0,1,2,3,4,5,6")
    setup(c)
    dialer, db = c.app.state.dialer, c.app.state.db
    job = c.post("/api/v1/outbound-calls", json=body(max_attempts=2), headers=h()).json()
    assert job["attempts"] == 1
    status_post(c, {"CallSid": "CAout", "CallStatus": "ringing", "SequenceNumber": "1"})
    status_post(c, {"CallSid": "CAout", "CallStatus": "no-answer", "SequenceNumber": "2"})
    j = run(db.get_outbound_job(ORG, job["id"]))
    assert j["status"] == "SCHEDULED" and j["last_result"] == "NO_ANSWER" and j["to_number"] == TO
    wait = j["next_attempt_at"] - datetime.now(timezone.utc)
    assert timedelta(minutes=14) < wait <= timedelta(minutes=15, seconds=5)

    assert run(dialer.tick(datetime.now(timezone.utc) + timedelta(minutes=1))) == 0  # aún no toca
    assert run(dialer.tick(datetime.now(timezone.utc) + timedelta(minutes=16))) == 1
    assert len(tw.calls) == 2 and tw.calls[1][0] == TO
    status_post(c, {"CallSid": "CAout2", "CallStatus": "busy", "SequenceNumber": "1"})
    j = run(db.get_outbound_job(ORG, job["id"]))
    assert j["status"] == "EXHAUSTED" and j["attempts"] == 2 and j["to_number"] is None  # el número se borra al terminar
    assert run(dialer.tick(datetime.now(timezone.utc) + timedelta(days=1))) == 0 and len(tw.calls) == 2


def test_answered_call_is_not_retried():
    c, tw = make(outbound_window_start="00:00", outbound_window_end="23:59", outbound_days="0,1,2,3,4,5,6")
    setup(c)
    job = c.post("/api/v1/outbound-calls", json=body(), headers=h()).json()
    for st, seq in (("ringing", "1"), ("in-progress", "2"), ("completed", "3")):
        status_post(c, {"CallSid": "CAout", "CallStatus": st, "SequenceNumber": seq})
    j = c.get(f"/api/v1/outbound-calls/{job['id']}", headers=h()).json()
    assert j["status"] == "COMPLETED" and j["to_masked"] is None and len(tw.calls) == 1


def test_provider_error_counts_as_an_attempt_and_retries():
    c, tw = make(outbound_window_start="00:00", outbound_window_end="23:59", outbound_days="0,1,2,3,4,5,6")
    setup(c)

    async def boom(*a, **k):
        raise RuntimeError("Twilio caído")

    tw.create_call = boom
    job = c.post("/api/v1/outbound-calls", json=body(max_attempts=3), headers=h()).json()
    assert job["status"] == "SCHEDULED" and job["attempts"] == 1 and job["last_result"] == "FAILED"


def test_legacy_endpoint_enforces_the_same_rules():
    c, tw = make(outbound_window_start="00:00", outbound_window_end="23:59", outbound_days="0,1,2,3,4,5,6")
    setup(c)
    assert c.post("/api/v1/telephony/calls", json={"to": TO}, headers=h()).status_code == 422  # sin consentimiento no hay llamada
    r = c.post("/api/v1/telephony/calls", json=body(), headers=h())
    assert r.status_code == 202 and r.json()["call_sid"] == "CAout" and len(tw.calls) == 1
