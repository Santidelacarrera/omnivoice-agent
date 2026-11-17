"""Etapa 5: reconocer a quien llama, consultar sus pedidos y enviarle una confirmación por SMS (solo a su número)."""
import asyncio
import base64
import json
import time
import uuid
from urllib.parse import urlencode

import pytest

from app.realtime.provider import FakeProvider
from app.security.auth import Principal, issue_token
from app.telephony.codec import ulaw_encode
from app.telephony.routes import caller_id_hash
from app.telephony.twilio import compute_signature
from app.tools.context import CALL_CONTEXT
from app.tools.handlers import InMemoryRepository, build_registry
from tests.test_telephony import PUBLIC, make, sine

ORG = str(uuid.uuid4())
CUSTOMER = "+34600000001"
OTHER_CALLER = "+34600000002"
OUR = "+34910000000"


def run(coro):
    return asyncio.run(coro)


def h(org=ORG, role="admin"):
    return {"Authorization": f"Bearer {issue_token('u', org, role)}"}


class Messenger:
    def __init__(self):
        self.sent = []

    async def send_sms(self, to, from_, body):
        self.sent.append((to, from_, body))
        return {"sid": "SM1"}


def phone_principal(number=CUSTOMER, org="o1"):
    return Principal(f"phone:{caller_id_hash(number)}", org, "customer")


# ---------- herramientas ----------
def test_lookup_and_register_customer_by_caller_id_only():
    repo = InMemoryRepository()
    reg = build_registry(repo)
    web = Principal("web-user", "o1", "customer")  # sesión web: sin número de teléfono
    assert run(reg.execute("lookup_customer", {}, web, "a1"))["data"] == {"found": False, "reason": "llamante_no_identificado"}
    assert run(reg.execute("register_customer", {"first_name": "Ana"}, web, "a2"))["data"]["registered"] is False

    p = phone_principal()
    assert run(reg.execute("lookup_customer", {}, p, "b1"))["data"] == {"found": False}
    assert run(reg.execute("register_customer", {"first_name": "Ana"}, p, "b2"))["data"] == {"registered": True, "first_name": "Ana"}
    assert run(reg.execute("lookup_customer", {}, p, "b3"))["data"] == {"found": True, "first_name": "Ana"}  # solo el nombre de pila
    assert run(reg.execute("register_customer", {"first_name": "Otra"}, p, "b4"))["data"]["first_name"] == "Ana"  # idempotente por número
    assert run(reg.execute("lookup_customer", {}, phone_principal(OTHER_CALLER), "b5"))["data"] == {"found": False}
    assert run(reg.execute("lookup_customer", {}, phone_principal(org="o2"), "b6"))["data"] == {"found": False}  # otra organización
    assert CUSTOMER not in str(repo.customers)  # no se guarda el número


def test_orders_are_visible_only_to_the_caller_they_belong_to():
    repo = InMemoryRepository()
    reg = build_registry(repo)
    run(repo.upsert_order("o1", "PED-1001", {"phone_hash": caller_id_hash(CUSTOMER), "status": "shipped", "summary": "1 chaqueta negra M",
                                             "total": 89.9, "eta_date": "2030-01-09"}))
    mine = run(reg.execute("check_order", {"order_id": "PED-1001"}, phone_principal(), "c1"))["data"]
    assert mine == {"found": True, "status": "shipped", "summary": "1 chaqueta negra M", "total": 89.9, "eta_date": "2030-01-09"}
    assert "phone_hash" not in json.dumps(mine)
    assert run(reg.execute("check_order", {"order_id": "PED-1001"}, phone_principal(OTHER_CALLER), "c2"))["data"] == {"found": False}  # ajeno = no existe
    assert run(reg.execute("check_order", {"order_id": "PED-1001"}, Principal("web", "o1", "customer"), "c3"))["data"] == {"found": False}
    assert run(reg.execute("check_order", {"order_id": "PED-1001"}, phone_principal(org="o2"), "c4"))["data"] == {"found": False}
    assert run(reg.execute("check_order", {"order_id": "PED-9999"}, phone_principal(), "c5"))["data"] == {"found": False}  # sin pistas
    assert run(reg.execute("check_order", {"order_id": "x"}, phone_principal(), "c6"))["error"] == "argumentos_invalidos"


def test_send_confirmation_goes_only_to_the_caller_and_is_capped():
    m = Messenger()
    reg = build_registry(InMemoryRepository(), lambda: m)
    p = phone_principal()
    ctx = {"caller": CUSTOMER, "our_number": OUR, "sms_sent": 0}
    token = CALL_CONTEXT.set(ctx)
    try:
        assert run(reg.execute("send_confirmation", {"message": "Su reserva es el lunes a las 20:00"}, p, "s1"))["data"] == {"sent": True}
        assert m.sent == [(CUSTOMER, OUR, "Su reserva es el lunes a las 20:00")]
        # el modelo no puede elegir el destino: no existe ese argumento
        bad = run(reg.execute("send_confirmation", {"message": "hola, hola", "to": "+34999999999"}, p, "s2"))
        assert bad["error"] == "argumentos_invalidos" and len(m.sent) == 1
        assert run(reg.execute("send_confirmation", {"message": "Segundo aviso de prueba"}, p, "s3"))["data"] == {"sent": True}
        assert run(reg.execute("send_confirmation", {"message": "Tercer aviso de prueba"}, p, "s4"))["data"] == {"sent": False, "reason": "limite_por_llamada"}
        assert len(m.sent) == 2
        assert run(reg.execute("send_confirmation", {"message": "Mismo id de llamada"}, p, "s1"))["data"] == {"sent": True}  # idempotente: no reenvía
        assert len(m.sent) == 2
    finally:
        CALL_CONTEXT.reset(token)


def test_send_confirmation_unavailable_without_context_or_provider():
    p = phone_principal()
    reg = build_registry(InMemoryRepository(), lambda: Messenger())
    assert run(reg.execute("send_confirmation", {"message": "Sin contexto de llamada"}, p, "n1"))["data"] == {"sent": False, "reason": "no_disponible"}
    token = CALL_CONTEXT.set({"caller": CUSTOMER, "our_number": OUR, "sms_sent": 0})
    try:
        none = build_registry(InMemoryRepository(), lambda: None)
        assert run(none.execute("send_confirmation", {"message": "Sin proveedor de SMS"}, p, "n2"))["data"] == {"sent": False, "reason": "no_disponible"}
    finally:
        CALL_CONTEXT.reset(token)


def test_register_customer_requires_confirmation_flag():
    reg = build_registry(InMemoryRepository())
    assert reg.get("register_customer").needs_confirmation and reg.get("send_confirmation").mutating
    assert not reg.get("lookup_customer").mutating and not reg.get("check_order").mutating


# ---------- API de importación y supresión ----------
def test_orders_import_api_stores_only_a_hash_and_validates():
    c, _ = make()
    body = {"phone": CUSTOMER, "status": "shipped", "summary": "1 chaqueta negra M", "total": 89.9, "eta_date": "2030-01-09"}
    assert c.put("/api/v1/orders/PED-1001", json=body, headers=h()).status_code == 200
    stored = c.app.state.repo.orders[(ORG, "PED-1001")]
    assert stored["phone_hash"] == caller_id_hash(CUSTOMER) and CUSTOMER not in json.dumps(stored, default=str)
    assert CUSTOMER not in c.put("/api/v1/orders/PED-1001", json=body, headers=h()).text  # idempotente y sin eco del número
    for role, code in (("manager", 200), ("operator", 403), ("analyst", 403), ("viewer", 403), ("customer", 403)):
        assert c.put("/api/v1/orders/PED-2002", json=body, headers=h(ORG, role)).status_code == code
    for bad in ({**body, "status": "inventado"}, {**body, "phone": "600000001"}, {**body, "eta_date": "2030-02-30"}, {**body, "total": -1},
                {**body, "extra": 1}, {"phone": CUSTOMER}):
        assert c.put("/api/v1/orders/PED-3003", json=bad, headers=h()).status_code == 422, bad
    assert c.put("/api/v1/orders/x", json=body, headers=h()).status_code == 422
    assert "order.upserted" in [e["action"] for e in c.get("/api/v1/audit-logs", headers=h()).json()["items"]]


def test_customers_list_and_erasure_are_isolated_and_admin_only():
    c, _ = make()
    repo = c.app.state.repo
    mine = run(repo.create_customer(ORG, "h1", "Ana"))
    run(repo.create_customer("otra-org", "h2", "Pedro"))
    assert [x["first_name"] for x in c.get("/api/v1/customers", headers=h()).json()["items"]] == ["Ana"]
    for role in ("manager", "operator", "analyst", "viewer", "customer"):
        assert c.get("/api/v1/customers", headers=h(ORG, role)).status_code == 403
    assert c.delete(f"/api/v1/customers/{mine['id']}", headers=h("otra-org")).status_code == 404  # no es de su organización
    assert c.delete(f"/api/v1/customers/{mine['id']}", headers=h()).status_code == 204
    assert c.delete(f"/api/v1/customers/{mine['id']}", headers=h()).status_code == 404
    assert c.delete("/api/v1/customers/no-uuid", headers=h()).status_code == 404
    assert "customer.deleted" in [e["action"] for e in c.get("/api/v1/audit-logs", headers=h()).json()["items"]]


# ---------- extremo a extremo por teléfono ----------
def phone_call(c, who, direction=None, sid="CA1"):
    params = {"CallSid": sid, "From": who, "To": OUR}
    if direction:
        params = {"CallSid": sid, "From": OUR, "To": who, "Direction": direction}
    r = c.post("/telephony/voice", content=urlencode(params), headers={
        "content-type": "application/x-www-form-urlencoded", "x-twilio-signature": compute_signature("tok", f"{PUBLIC}/telephony/voice", params)})
    return r.text.split('name="ticket" value="')[1].split('"')[0]


def drive(c, ticket, sid="CA1"):
    frame = base64.b64encode(ulaw_encode(sine(8000, 20))).decode()
    with c.websocket_connect("/ws/telephony") as ws:
        ws.send_text(json.dumps({"event": "start", "start": {"streamSid": "MZ1", "callSid": sid, "customParameters": {"ticket": ticket, "call_sid": sid}}}))
        for _ in range(3):
            ws.send_text(json.dumps({"event": "media", "streamSid": "MZ1", "media": {"payload": frame}}))
        time.sleep(0.6)
        ws.send_text(json.dumps({"event": "stop"}))


def script(*calls):
    return [{"type": "tool_call", "call_id": f"c{i}", "name": n, "arguments": json.dumps(a)} for i, (n, a) in enumerate(calls)]


def test_phone_call_sees_its_own_order_and_gets_the_sms_on_the_same_number():
    prov = FakeProvider(respond_on_audio=True, script=script(
        ("check_order", {"order_id": "PED-1001"}), ("send_confirmation", {"message": "Su pedido PED-1001 va en camino"})))
    c, tw = make(prov)
    c.__enter__()
    c.put("/api/v1/orders/PED-1001", json={"phone": CUSTOMER, "status": "shipped", "summary": "1 chaqueta", "total": 89.9}, headers=h("00000000-0000-0000-0000-000000000001"))
    drive(c, phone_call(c, CUSTOMER))
    out = {cid: r for cid, r in prov.tool_results}
    assert out["c0"]["data"]["found"] is True and out["c0"]["data"]["status"] == "shipped"
    assert tw.sms == [(CUSTOMER, OUR, "Su pedido PED-1001 va en camino")]  # al número que llamó, desde la línea marcada


def test_another_callers_order_is_not_found_and_outbound_identifies_the_callee():
    prov = FakeProvider(respond_on_audio=True, script=script(("check_order", {"order_id": "PED-1001"})))
    c, tw = make(prov)
    c.__enter__()
    c.put("/api/v1/orders/PED-1001", json={"phone": CUSTOMER, "status": "shipped", "summary": "1 chaqueta"}, headers=h("00000000-0000-0000-0000-000000000001"))
    drive(c, phone_call(c, OTHER_CALLER))
    assert prov.tool_results[0][1]["data"] == {"found": False}  # otro número: el pedido no existe para él

    prov2 = FakeProvider(respond_on_audio=True, script=script(("check_order", {"order_id": "PED-1001"}), ("send_confirmation", {"message": "Aviso al cliente llamado"})))
    c2, tw2 = make(prov2)
    c2.__enter__()
    c2.put("/api/v1/orders/PED-1001", json={"phone": CUSTOMER, "status": "shipped", "summary": "1 chaqueta"}, headers=h("00000000-0000-0000-0000-000000000001"))
    drive(c2, phone_call(c2, CUSTOMER, direction="outbound-api", sid="CA2"), sid="CA2")  # llamada saliente: el cliente es el destinatario
    assert prov2.tool_results[0][1]["data"]["found"] is True
    assert tw2.sms == [(CUSTOMER, OUR, "Aviso al cliente llamado")]


def test_caller_number_is_not_persisted_in_calls_or_audit_or_webhooks():
    prov = FakeProvider(respond_on_audio=True, script=script(("lookup_customer", {})))
    c, tw = make(prov)
    c.__enter__()
    drive(c, phone_call(c, CUSTOMER))
    db = c.app.state.db
    dump = json.dumps([db.calls, db.call_events, db.audits, db.webhook_deliveries], default=str)
    assert CUSTOMER not in dump and CUSTOMER.lstrip("+") not in dump
    assert prov.tool_results[0][1]["data"] == {"found": False}


@pytest.mark.parametrize("name", ["lookup_customer", "register_customer", "check_order", "send_confirmation"])
def test_new_tools_are_listed_for_agents(name):
    assert name in {t["name"] for t in build_registry(InMemoryRepository()).schemas()}
