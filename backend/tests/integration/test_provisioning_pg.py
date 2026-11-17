"""T2.6 contra PostgreSQL real: alta de organizaciones y miembros con el rol de administración."""
import asyncio
import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app import cli
from app.services import provisioning as pv

ADMIN_URL = os.environ.get("TEST_ADMIN_DATABASE_URL")
APP_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(not (ADMIN_URL and APP_URL), reason="requiere TEST_ADMIN_DATABASE_URL y TEST_DATABASE_URL")


def run(coro_fn):
    async def main():
        engine = create_async_engine(ADMIN_URL)
        try:
            return await coro_fn(engine)
        finally:
            await engine.dispose()

    return asyncio.run(main())


def test_create_org_members_and_last_owner_rule():
    tag = uuid.uuid4().hex[:8]
    org = run(lambda e: pv.create_organization(e, f"Clínica {tag}", f"Dueño.{tag}@Clinica.CL"))
    assert org["owner_email"] == f"dueño.{tag}@clinica.cl"  # normalizado a minúsculas
    oid = org["org_id"]
    m = run(lambda e: pv.set_member(e, oid, f"ana.{tag}@clinica.cl", "manager"))
    assert m["role"] == "manager" and m["previous_role"] is None
    m2 = run(lambda e: pv.set_member(e, oid, f"ana.{tag}@clinica.cl", "analyst"))
    assert m2["previous_role"] == "manager" and m2["user_id"] == m["user_id"]  # mismo usuario, rol cambiado
    members = run(lambda e: pv.list_members(e, oid))
    assert sorted(x["role"] for x in members) == ["analyst", "owner"]
    with pytest.raises(pv.ProvisioningError, match="último owner"):
        run(lambda e: pv.set_member(e, oid, org["owner_email"], "admin"))
    run(lambda e: pv.set_member(e, oid, f"ana.{tag}@clinica.cl", "owner"))  # con dos owners ya se puede degradar
    assert run(lambda e: pv.set_member(e, oid, org["owner_email"], "admin"))["previous_role"] == "owner"


def test_validation_and_unknown_org():
    with pytest.raises(pv.ProvisioningError):
        run(lambda e: pv.create_organization(e, "x", "a@b.cl"))
    with pytest.raises(pv.ProvisioningError):
        run(lambda e: pv.create_organization(e, "Clínica", "no-es-email"))
    with pytest.raises(pv.ProvisioningError):
        run(lambda e: pv.set_member(e, str(uuid.uuid4()), "a@b.cl", "viewer"))
    with pytest.raises(pv.ProvisioningError):
        run(lambda e: pv.set_member(e, "no-uuid", "a@b.cl", "viewer"))


def test_app_role_cannot_touch_identity_tables_and_new_org_is_usable():
    """omni_app sigue sin acceso a users/memberships, pero ve su organización nueva (retención) y sus datos están aislados."""
    org = run(lambda e: pv.create_organization(e, f"Aislada {uuid.uuid4().hex[:6]}", "x@y.cl", retention_days=7))

    async def probe():
        eng = create_async_engine(APP_URL)
        try:
            async with eng.begin() as c:
                await c.execute(text("SELECT set_config('app.org_id', :o, true)"), {"o": org["org_id"]})
                days = (await c.execute(text("SELECT retention_days FROM organizations WHERE id = :o"), {"o": org["org_id"]})).scalar()
                n = (await c.execute(text("SELECT count(*) FROM agents"))).scalar()
            denied = False
            try:
                async with eng.begin() as c:
                    await c.execute(text("SELECT * FROM users"))
            except Exception:  # noqa: BLE001
                denied = True
            return days, n, denied
        finally:
            await eng.dispose()

    assert asyncio.run(probe()) == (7, 0, True)


def test_cli_requires_admin_url(monkeypatch, capsys):
    monkeypatch.delenv("ADMIN_DATABASE_URL", raising=False)
    assert cli.main(["create-org", "--name", "Clínica", "--owner-email", "a@b.cl"]) == 2
    assert "ADMIN_DATABASE_URL" in capsys.readouterr().err


def test_cli_roundtrip(monkeypatch, capsys):
    monkeypatch.setenv("ADMIN_DATABASE_URL", ADMIN_URL)
    assert cli.main(["create-org", "--name", "Vía CLI", "--owner-email", "no-es-email"]) == 2  # error de validación limpio, sin traza
    assert "Error:" in capsys.readouterr().err
    assert cli.main(["create-org", "--name", "Vía CLI", "--owner-email", "cli@via.cl"]) == 0
    out = capsys.readouterr().out
    assert '"org_id"' in out
