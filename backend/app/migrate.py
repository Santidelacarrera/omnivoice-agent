"""Migraciones con versión (T2.7).

  ADMIN_DATABASE_URL=postgresql+asyncpg://omni:...@host/omnivoice python -m app.migrate status|up|baseline --through 004

Reglas:
- Aplica `migrations/NNN_*.sql` en orden; cada archivo en su propia transacción junto con su fila en `schema_migrations`
  (si falla, no queda a medias ni registrada).
- Guarda el checksum SHA-256: si un archivo ya aplicado cambia, se detiene (las migraciones aplicadas son inmutables;
  los cambios van en un archivo nuevo).
- Un candado consultivo evita que dos instancias migren a la vez.
- Una base creada antes de este mecanismo (tiene `organizations` pero no `schema_migrations`) NO se migra sola:
  hay que declarar hasta qué versión ya estaba con `baseline --through NNN`. Es deliberado, para no reejecutar DDL.
Usa el rol de migraciones (dueño de las tablas), nunca `omni_app`.
"""
import argparse
import asyncio
import hashlib
import os
import re
import sys
from pathlib import Path

import asyncpg

LOCK_KEY = 726_001  # arbitrario pero fijo para este proyecto
FILE_RE = re.compile(r"^(\d{3})_[\w\-]+\.sql$")
DEFAULT_DIR = Path(__file__).resolve().parents[2] / "migrations"


class MigrationError(RuntimeError):
    """Problema de migración con mensaje seguro de mostrar al operador."""


def discover(directory: Path) -> list[tuple[str, Path, str]]:
    """(versión, ruta, checksum) ordenadas; versiones duplicadas son un error."""
    found: dict[str, Path] = {}
    for f in sorted(directory.glob("*.sql")):
        m = FILE_RE.match(f.name)
        if not m:
            raise MigrationError(f"Nombre de migración inválido: {f.name} (usa NNN_descripcion.sql)")
        if m.group(1) in found:
            raise MigrationError(f"Versión {m.group(1)} duplicada: {found[m.group(1)].name} y {f.name}")
        found[m.group(1)] = f
    return [(v, p, hashlib.sha256(p.read_bytes()).hexdigest()) for v, p in sorted(found.items())]


def _dsn(url: str) -> str:
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


async def _ensure_table(conn) -> None:
    await conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, name TEXT NOT NULL, "
        "checksum TEXT NOT NULL, applied_at TIMESTAMPTZ NOT NULL DEFAULT now(), baseline BOOLEAN NOT NULL DEFAULT false)")


async def _applied(conn) -> dict[str, tuple[str, bool]]:
    rows = await conn.fetch("SELECT version, checksum, baseline FROM schema_migrations")
    return {r["version"]: (r["checksum"], r["baseline"]) for r in rows}


def _check_drift(files, applied) -> None:
    known = {v for v, _, _ in files}
    for v in applied:
        if v not in known:
            raise MigrationError(f"La base tiene la migración {v}, que no existe en el directorio (¿versión de código antigua?)")
    for v, path, checksum in files:
        if v in applied and applied[v][0] != checksum and not applied[v][1]:
            raise MigrationError(f"{path.name} cambió después de aplicarse; las migraciones aplicadas son inmutables. "
                                 "Crea una migración nueva.")


async def _legacy_schema(conn) -> bool:
    return bool(await conn.fetchval("SELECT to_regclass('public.organizations') IS NOT NULL"))


async def status(url: str, directory: Path = DEFAULT_DIR) -> list[dict]:
    files = discover(directory)
    conn = await asyncpg.connect(_dsn(url))
    try:
        has_table = await conn.fetchval("SELECT to_regclass('public.schema_migrations') IS NOT NULL")
        applied = await _applied(conn) if has_table else {}
        if not has_table and await _legacy_schema(conn):
            raise MigrationError("Base existente sin registro de versiones: ejecuta `baseline --through NNN` (la última que ya tenga).")
        _check_drift(files, applied)
        return [{"version": v, "file": p.name, "state": "applied" if v in applied else "pending"} for v, p, _ in files]
    finally:
        await conn.close()


async def up(url: str, directory: Path = DEFAULT_DIR, target: str | None = None) -> list[str]:
    files = discover(directory)
    conn = await asyncpg.connect(_dsn(url))
    done: list[str] = []
    try:
        if not await conn.fetchval("SELECT pg_try_advisory_lock($1)", LOCK_KEY):
            raise MigrationError("Otra instancia está migrando ahora mismo")
        has_table = await conn.fetchval("SELECT to_regclass('public.schema_migrations') IS NOT NULL")
        if not has_table and await _legacy_schema(conn):
            raise MigrationError("Base existente sin registro de versiones: ejecuta `baseline --through NNN` antes de `up`.")
        await _ensure_table(conn)
        applied = await _applied(conn)
        _check_drift(files, applied)
        for v, path, checksum in files:
            if v in applied or (target and v > target):
                continue
            try:
                async with conn.transaction():
                    await conn.execute(path.read_text(encoding="utf-8"))
                    await conn.execute("INSERT INTO schema_migrations (version, name, checksum) VALUES ($1, $2, $3)",
                                       v, path.name, checksum)
            except asyncpg.PostgresError as exc:
                raise MigrationError(f"{path.name} falló y se revirtió: {exc}") from exc
            done.append(path.name)
        return done
    finally:
        await conn.execute("SELECT pg_advisory_unlock($1)", LOCK_KEY)
        await conn.close()


async def baseline(url: str, through: str, directory: Path = DEFAULT_DIR) -> list[str]:
    """Registra como aplicadas (sin ejecutarlas) las migraciones hasta `through` en una base que ya las tenía."""
    files = discover(directory)
    if through not in {v for v, _, _ in files}:
        raise MigrationError(f"La versión {through} no existe en {directory}")
    conn = await asyncpg.connect(_dsn(url))
    try:
        if not await conn.fetchval("SELECT to_regclass('public.organizations') IS NOT NULL"):
            raise MigrationError("La base está vacía: usa `up`, no `baseline`")
        await _ensure_table(conn)
        if await _applied(conn):
            raise MigrationError("La base ya tiene versiones registradas; baseline solo sirve la primera vez")
        marked = []
        async with conn.transaction():
            for v, path, checksum in files:
                if v <= through:
                    await conn.execute("INSERT INTO schema_migrations (version, name, checksum, baseline) VALUES ($1, $2, $3, true)",
                                       v, path.name, checksum)
                    marked.append(path.name)
        return marked
    finally:
        await conn.close()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="app.migrate")
    p.add_argument("--dir", default=os.environ.get("MIGRATIONS_DIR", str(DEFAULT_DIR)))
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    u = sub.add_parser("up")
    u.add_argument("--target", help="aplica solo hasta esta versión (p. ej. 004)")
    b = sub.add_parser("baseline")
    b.add_argument("--through", required=True, help="última versión que la base ya tenía (p. ej. 004)")
    a = p.parse_args(argv)
    url = os.environ.get("ADMIN_DATABASE_URL")
    if not url:
        print("Falta ADMIN_DATABASE_URL (rol de migraciones, no omni_app)", file=sys.stderr)
        return 2
    d = Path(a.dir)
    try:
        if a.cmd == "status":
            for r in asyncio.run(status(url, d)):
                print(f"{r['state']:8} {r['file']}")
        elif a.cmd == "up":
            done = asyncio.run(up(url, d, a.target))
            print("\n".join(f"aplicada {n}" for n in done) if done else "Nada que aplicar")
        else:
            print("\n".join(f"registrada {n}" for n in asyncio.run(baseline(url, a.through, d))))
    except MigrationError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except (OSError, asyncpg.PostgresError) as exc:
        print(f"Error de conexión o base de datos: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
