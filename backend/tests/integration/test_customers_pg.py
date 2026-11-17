"""Migración 015 contra PostgreSQL real: clientes y pedidos con RLS; el agente solo ve lo de quien llama."""
import os
import uuid

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings, get_settings
from app.main import create_app
from app.security.auth import Principal, issue_token
from app.telephony.routes import caller_id_hash
from app.tools.handlers import build_registry

PG_URL = os.environ.get("TEST_DATABASE_URL")
REDIS_URL = os.environ.get("TEST_REDIS_URL")
DEMO = "00000000-0000-0000-0000-000000000001"
OTHER = "00000000-0000-0000-0000-0000000000ee"

pytestmark = pytest.mark.skipif(not (PG_URL and REDIS_URL), reason="requiere TEST_DATABASE_URL y TEST_REDIS_URL")


def _h(org, role="admin"):
    return {"Authorization": f"Bearer {issue_token('u-cu', org, role)}"}


def test_customers_and_orders_on_postgres():
    s = Settings(environment="test", jwt_secret=get_settings().jwt_secret, persistence_backend="postgres", state_backend="redis",
                 database_url=PG_URL, redis_url=REDIS_URL, vad_backend="energy", retention_job_enabled=False)
    number = "+3460" + str(uuid.uuid4().int)[:7]
    stranger = "+3461" + str(uuid.uuid4().int)[:7]
    order_id = "PED-" + uuid.uuid4().hex[:8].upper()
    with TestClient(create_app(s)) as c:
        reg = build_registry(c.app.state.repo)
        mine = Principal(f"phone:{caller_id_hash(number)}", DEMO, "customer")
        other_caller = Principal(f"phone:{caller_id_hash(stranger)}", DEMO, "customer")
        other_org = Principal(f"phone:{caller_id_hash(number)}", OTHER, "customer")

        def call(p, name, args, cid):
            return c.portal.call(reg.execute, name, args, p, cid)

        assert call(mine, "lookup_customer", {}, "pg-1")["data"] == {"found": False}
        assert call(mine, "register_customer", {"first_name": "Ana"}, "pg-2")["data"] == {"registered": True, "first_name": "Ana"}
        assert call(mine, "register_customer", {"first_name": "Otra"}, "pg-3")["data"]["first_name"] == "Ana"  # único por número
        assert call(mine, "lookup_customer", {}, "pg-4")["data"] == {"found": True, "first_name": "Ana"}
        assert call(other_caller, "lookup_customer", {}, "pg-5")["data"] == {"found": False}
        assert call(other_org, "lookup_customer", {}, "pg-6")["data"] == {"found": False}  # RLS: otra organización

        body = {"phone": number, "status": "shipped", "summary": "1 chaqueta negra M", "total": 89.9, "eta_date": "2030-01-09"}
        assert c.put(f"/api/v1/orders/{order_id}", headers=_h(DEMO), json=body).status_code == 200
        assert c.put(f"/api/v1/orders/{order_id}", headers=_h(DEMO), json={**body, "status": "delivered"}).status_code == 200  # actualiza
        got = call(mine, "check_order", {"order_id": order_id}, "pg-7")["data"]
        assert got["found"] and got["status"] == "delivered" and got["total"] == 89.9 and got["eta_date"] == "2030-01-09"
        assert call(other_caller, "check_order", {"order_id": order_id}, "pg-8")["data"] == {"found": False}
        assert call(other_org, "check_order", {"order_id": order_id}, "pg-9")["data"] == {"found": False}

        items = c.get("/api/v1/customers", headers=_h(DEMO)).json()["items"]
        cid = next(i["id"] for i in items if i["first_name"] == "Ana")
        assert c.get("/api/v1/customers", headers=_h(OTHER)).json()["items"] == []
        assert c.delete(f"/api/v1/customers/{cid}", headers=_h(OTHER)).status_code == 404
        assert c.delete(f"/api/v1/customers/{cid}", headers=_h(DEMO)).status_code == 204
        assert call(mine, "lookup_customer", {}, "pg-10")["data"] == {"found": False}  # suprimido
