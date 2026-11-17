"""T2.7 contra PostgreSQL real: el ejecutor de migraciones usa una base temporal por prueba."""
import asyncio
import os
import uuid
from pathlib import Path

import asyncpg
import pytest

from app import migrate

ADMIN_URL = os.environ.get("TEST_ADMIN_DATABASE_URL")
pytestmark = pytest.mark.skipif(not ADMIN_URL, reason="requiere TEST_ADMIN_DATABASE_URL")


def _dsn(db: str | None = None) -> str:
    base = ADMIN_URL.replace("postgresql+asyncpg://", "postgresql://", 1)
    return base.rsplit("/", 1)[0] + "/" + (db or "postgres")


@pytest.fixture
def db():
    name = "mig_" + uuid.uuid4().hex[:10]

    async def make():
        c = await asyncpg.connect(_dsn())
        await c.execute(f'CREATE DATABASE "{name}"')
        await c.close()

    async def drop():
        c = await asyncpg.connect(_dsn())
        await c.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
        await c.close()

    asyncio.run(make())
    yield _dsn(name).replace("postgresql://", "postgresql+asyncpg://", 1)
    asyncio.run(drop())


def sql_dir(tmp_path: Path, files: dict[str, str]) -> Path:
    for n, body in files.items():
        (tmp_path / n).write_text(body, encoding="utf-8")
    return tmp_path


A = "CREATE TABLE organizations (id INT);\nINSERT INTO organizations VALUES (1);"
B = "CREATE TABLE t2 (x INT);"


def test_up_applies_in_order_and_is_idempotent(db, tmp_path):
    d = sql_dir(tmp_path, {"002_b.sql": B, "001_a.sql": A})
    assert asyncio.run(migrate.up(db, d)) == ["001_a.sql", "002_b.sql"]
    assert asyncio.run(migrate.up(db, d)) == []
    assert [r["state"] for r in asyncio.run(migrate.status(db, d))] == ["applied", "applied"]


def test_new_file_applies_only_pending(db, tmp_path):
    d = sql_dir(tmp_path, {"001_a.sql": A})
    asyncio.run(migrate.up(db, d))
    sql_dir(tmp_path, {"002_b.sql": B})
    assert [r["state"] for r in asyncio.run(migrate.status(db, d))] == ["applied", "pending"]
    assert asyncio.run(migrate.up(db, d)) == ["002_b.sql"]


def test_target_limits_versions(db, tmp_path):
    d = sql_dir(tmp_path, {"001_a.sql": A, "002_b.sql": B})
    assert asyncio.run(migrate.up(db, d, target="001")) == ["001_a.sql"]


def test_failure_rolls_back_file_and_is_not_recorded(db, tmp_path):
    d = sql_dir(tmp_path, {"001_a.sql": A, "002_bad.sql": "CREATE TABLE ok_before_fail (x INT); SELECT 1/0;"})
    with pytest.raises(migrate.MigrationError, match="002_bad.sql falló y se revirtió"):
        asyncio.run(migrate.up(db, d))

    async def probe():
        c = await asyncpg.connect(db.replace("+asyncpg", ""))
        try:
            return (await c.fetchval("SELECT to_regclass('public.ok_before_fail') IS NOT NULL"),
                    [r["version"] for r in await c.fetch("SELECT version FROM schema_migrations")])
        finally:
            await c.close()

    assert asyncio.run(probe()) == (False, ["001"])


def test_checksum_drift_is_rejected(db, tmp_path):
    d = sql_dir(tmp_path, {"001_a.sql": A})
    asyncio.run(migrate.up(db, d))
    (d / "001_a.sql").write_text(A + "\n-- editada", encoding="utf-8")
    with pytest.raises(migrate.MigrationError, match="inmutables"):
        asyncio.run(migrate.up(db, d))
    with pytest.raises(migrate.MigrationError, match="inmutables"):
        asyncio.run(migrate.status(db, d))


def test_legacy_database_requires_baseline_then_runs_only_new(db, tmp_path):
    d = sql_dir(tmp_path, {"001_a.sql": A})
    asyncio.run(migrate.up(db, d))

    async def forget():  # simula una base creada antes del mecanismo
        c = await asyncpg.connect(db.replace("+asyncpg", ""))
        await c.execute("DROP TABLE schema_migrations")
        await c.close()

    asyncio.run(forget())
    sql_dir(tmp_path, {"002_b.sql": B})
    with pytest.raises(migrate.MigrationError, match="baseline"):
        asyncio.run(migrate.up(db, d))
    assert asyncio.run(migrate.baseline(db, "001", d)) == ["001_a.sql"]
    assert asyncio.run(migrate.up(db, d)) == ["002_b.sql"]
    with pytest.raises(migrate.MigrationError, match="solo sirve la primera vez"):
        asyncio.run(migrate.baseline(db, "001", d))


def test_baseline_on_empty_db_and_unknown_version(db, tmp_path):
    d = sql_dir(tmp_path, {"001_a.sql": A})
    with pytest.raises(migrate.MigrationError, match="vacía"):
        asyncio.run(migrate.baseline(db, "001", d))
    with pytest.raises(migrate.MigrationError, match="no existe"):
        asyncio.run(migrate.baseline(db, "009", d))


def test_discovery_rejects_bad_names_and_duplicates(tmp_path):
    with pytest.raises(migrate.MigrationError, match="inválido"):
        migrate.discover(sql_dir(tmp_path, {"1_x.sql": "SELECT 1"}))
    d2 = tmp_path / "dup"
    d2.mkdir()
    with pytest.raises(migrate.MigrationError, match="duplicada"):
        migrate.discover(sql_dir(d2, {"001_a.sql": "SELECT 1", "001_b.sql": "SELECT 1"}))


def test_concurrent_run_is_refused(db, tmp_path):
    d = sql_dir(tmp_path, {"001_a.sql": A})

    async def scenario():
        holder = await asyncpg.connect(db.replace("+asyncpg", ""))
        await holder.fetchval("SELECT pg_advisory_lock($1)", migrate.LOCK_KEY)
        try:
            with pytest.raises(migrate.MigrationError, match="Otra instancia"):
                await migrate.up(db, d)
        finally:
            await holder.close()

    asyncio.run(scenario())


def test_cli_messages(db, tmp_path, monkeypatch, capsys):
    d = sql_dir(tmp_path, {"001_a.sql": A})
    monkeypatch.delenv("ADMIN_DATABASE_URL", raising=False)
    assert migrate.main(["--dir", str(d), "status"]) == 2
    monkeypatch.setenv("ADMIN_DATABASE_URL", db)
    assert migrate.main(["--dir", str(d), "up"]) == 0
    assert "aplicada 001_a.sql" in capsys.readouterr().out
    assert migrate.main(["--dir", str(d), "up"]) == 0
    assert "Nada que aplicar" in capsys.readouterr().out
