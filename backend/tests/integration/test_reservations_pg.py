"""Etapa 5 contra PostgreSQL real: modificar y cancelar reservas (migración 010) con RLS."""
import os

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings, get_settings
from app.main import create_app
from app.security.auth import Principal
from app.tools.handlers import build_registry

PG_URL = os.environ.get("TEST_DATABASE_URL")
REDIS_URL = os.environ.get("TEST_REDIS_URL")
DEMO = "00000000-0000-0000-0000-000000000001"
OTHER = "00000000-0000-0000-0000-0000000000ee"

pytestmark = pytest.mark.skipif(not (PG_URL and REDIS_URL), reason="requiere TEST_DATABASE_URL y TEST_REDIS_URL")


def test_modify_and_cancel_reservation_on_postgres():
    s = Settings(environment="test", jwt_secret=get_settings().jwt_secret, persistence_backend="postgres", state_backend="redis",
                 database_url=PG_URL, redis_url=REDIS_URL, vad_backend="energy", retention_job_enabled=False)
    with TestClient(create_app(s)) as c:
        reg = build_registry(c.app.state.repo)
        mine, other = Principal("u", DEMO, "customer"), Principal("u", OTHER, "customer")

        def call(p, name, args, cid):
            return c.portal.call(reg.execute, name, args, p, cid)

        created = call(mine, "create_reservation", {"name": "Ana", "date": "2030-01-07", "time": "20:00", "party_size": 2}, "pg-1")
        rid = created["data"]["reservation_id"]
        assert created["data"]["status"] == "confirmed"

        assert call(mine, "modify_reservation", {"reservation_id": rid, "name": "Pedro", "new_time": "21:00"}, "pg-2")["data"]["modified"] is False
        ok = call(mine, "modify_reservation", {"reservation_id": rid, "name": "ana", "new_time": "21:30", "new_party_size": 4}, "pg-3")
        assert ok["data"]["modified"] is True
        got = call(mine, "check_reservation", {"reservation_id": rid}, "pg-4")["data"]
        assert got["time"] == "21:30" and got["party_size"] == 4 and got["status"] == "confirmed"

        # otra organización no ve ni cancela esa reserva (RLS)
        assert call(other, "check_reservation", {"reservation_id": rid}, "pg-5")["data"] == {"found": False}
        assert call(other, "cancel_reservation", {"reservation_id": rid, "name": "Ana"}, "pg-6")["data"]["cancelled"] is False

        assert call(mine, "cancel_reservation", {"reservation_id": rid, "name": "Ana"}, "pg-7")["data"] == {"cancelled": True, "already": False}
        assert call(mine, "cancel_reservation", {"reservation_id": rid, "name": "Ana"}, "pg-8")["data"]["already"] is True
        assert call(mine, "modify_reservation", {"reservation_id": rid, "name": "Ana", "new_time": "22:00"}, "pg-9")["data"]["reason"] == "reserva_cancelada"
        assert call(mine, "check_reservation", {"reservation_id": rid}, "pg-10")["data"]["status"] == "cancelled"  # la fila se conserva
