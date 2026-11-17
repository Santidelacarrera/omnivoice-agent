"""Herramientas de operador sin base de datos: validaciones, descubrimiento de migraciones y mensajes del CLI."""
import asyncio
import hashlib

import pytest

from app import cli, migrate
from app.services import provisioning as pv


def run(coro):
    return asyncio.run(coro)


# ---------- aprovisionamiento: toda validación ocurre antes de tocar la base (engine=None) ----------
@pytest.mark.parametrize("name,email,days", [
    ("x", "a@b.cl", 30), ("n" * 121, "a@b.cl", 30), ("Clínica", "no-es-email", 30),
    ("Clínica", "a b@c.cl", 30), ("Clínica", "a@b.cl", 0), ("Clínica", "a@b.cl", 4000),
])
def test_create_org_rejects_bad_input(name, email, days):
    with pytest.raises(pv.ProvisioningError):
        run(pv.create_organization(None, name, email, days))


@pytest.mark.parametrize("org,email,role", [
    ("no-uuid", "a@b.cl", "viewer"),
    ("00000000-0000-0000-0000-000000000001", "malo", "viewer"),
    ("00000000-0000-0000-0000-000000000001", "a@b.cl", "superadmin"),
])
def test_set_member_rejects_bad_input(org, email, role):
    with pytest.raises(pv.ProvisioningError):
        run(pv.set_member(None, org, email, role))


def test_list_members_rejects_bad_uuid():
    with pytest.raises(pv.ProvisioningError):
        run(pv.list_members(None, "no-uuid"))


def test_email_is_normalized():
    assert pv._email("  Ana@Clinica.CL ") == "ana@clinica.cl"


# ---------- CLI ----------
def test_cli_provisioning_needs_admin_url(monkeypatch, capsys):
    monkeypatch.delenv("ADMIN_DATABASE_URL", raising=False)
    for argv in (["create-org", "--name", "Clínica", "--owner-email", "a@b.cl"],
                 ["set-member", "--org", "x", "--email", "a@b.cl", "--role", "viewer"],
                 ["list-members", "--org", "x"]):
        assert cli.main(argv) == 2
    assert "ADMIN_DATABASE_URL" in capsys.readouterr().err


def test_cli_issue_token_validation(capsys):
    assert cli.main(["issue-token", "--org", "no-uuid", "--user", "u"]) == 2
    assert cli.main(["issue-token", "--org", "00000000-0000-0000-0000-000000000001", "--user", "u", "--ttl", "5"]) == 2
    assert cli.main(["issue-token", "--org", "00000000-0000-0000-0000-000000000001", "--user", "u", "--role", "analyst"]) == 0
    assert capsys.readouterr().out.count(".") == 2  # un JWT


def test_cli_rejects_unknown_role():
    with pytest.raises(SystemExit):
        cli.main(["set-member", "--org", "x", "--email", "a@b.cl", "--role", "dios"])


# ---------- migraciones: descubrimiento y detección de cambios, sin base ----------
def test_discover_orders_and_hashes(tmp_path):
    (tmp_path / "002_b.sql").write_text("SELECT 2", encoding="utf-8")
    (tmp_path / "001_a.sql").write_text("SELECT 1", encoding="utf-8")
    found = migrate.discover(tmp_path)
    assert [v for v, _, _ in found] == ["001", "002"]
    assert found[0][2] == hashlib.sha256(b"SELECT 1").hexdigest()


def test_discover_rejects_bad_names_and_duplicates(tmp_path):
    (tmp_path / "1_x.sql").write_text("SELECT 1", encoding="utf-8")
    with pytest.raises(migrate.MigrationError, match="inválido"):
        migrate.discover(tmp_path)
    d = tmp_path / "dup"
    d.mkdir()
    (d / "001_a.sql").write_text("SELECT 1", encoding="utf-8")
    (d / "001_b.sql").write_text("SELECT 1", encoding="utf-8")
    with pytest.raises(migrate.MigrationError, match="duplicada"):
        migrate.discover(d)


def test_real_migrations_directory_is_well_formed():
    files = migrate.discover(migrate.DEFAULT_DIR)
    assert [v for v, _, _ in files] == sorted(v for v, _, _ in files) and len(files) >= 5


def test_drift_detection(tmp_path):
    (tmp_path / "001_a.sql").write_text("SELECT 1", encoding="utf-8")
    files = migrate.discover(tmp_path)
    good = {"001": (files[0][2], False)}
    migrate._check_drift(files, good)
    with pytest.raises(migrate.MigrationError, match="inmutables"):
        migrate._check_drift(files, {"001": ("otro", False)})
    migrate._check_drift(files, {"001": ("otro", True)})  # baseline: sin checksum real que comparar
    with pytest.raises(migrate.MigrationError, match="no existe en el directorio"):
        migrate._check_drift(files, {**good, "009": ("x", False)})


def test_migrate_cli_needs_url(monkeypatch, capsys):
    monkeypatch.delenv("ADMIN_DATABASE_URL", raising=False)
    assert migrate.main(["status"]) == 2
    assert "ADMIN_DATABASE_URL" in capsys.readouterr().err


def test_migrate_cli_reports_unreachable_database(monkeypatch, capsys, tmp_path):
    (tmp_path / "001_a.sql").write_text("SELECT 1", encoding="utf-8")
    monkeypatch.setenv("ADMIN_DATABASE_URL", "postgresql+asyncpg://x:y@127.0.0.1:1/none")
    assert migrate.main(["--dir", str(tmp_path), "up"]) == 1
    assert "Error" in capsys.readouterr().err
