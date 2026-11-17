"""Utilidades de operador. Uso:  python -m app.cli issue-token --org <uuid> --user <id> --role admin [--ttl 3600]
  python -m app.cli create-org --name "Clínica Sur" --owner-email dueno@clinica.cl
  python -m app.cli set-member --org <uuid> --email persona@clinica.cl --role manager
  python -m app.cli list-members --org <uuid>
Los tres últimos requieren ADMIN_DATABASE_URL (rol de migraciones, NO el de la app): ver app/services/provisioning.py.

Sirve para emitir tokens mientras no hay un proveedor de identidad externo (p. ej. un administrador o un servicio
de confianza). Usa el mismo JWT_SECRET que el backend, así que ejecútalo dentro del contenedor:
  docker compose exec backend python -m app.cli issue-token --org <uuid> --user admin1 --role admin
"""
import argparse
import asyncio
import json
import os
import sys
import uuid

from app.security.auth import ROLE_PERMISSIONS, issue_token


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="app.cli")
    sub = p.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("issue-token", help="Emite un JWT para una organización")
    t.add_argument("--org", required=True, help="UUID de la organización")
    t.add_argument("--user", required=True)
    t.add_argument("--role", choices=sorted(ROLE_PERMISSIONS), default="customer")
    t.add_argument("--ttl", type=int, default=3600, help="segundos de vigencia (60 a 86400)")
    c = sub.add_parser("create-org", help="Crea una organización y su owner")
    c.add_argument("--name", required=True)
    c.add_argument("--owner-email", required=True)
    c.add_argument("--retention-days", type=int, default=30)
    m = sub.add_parser("set-member", help="Añade un usuario a una organización o cambia su rol")
    m.add_argument("--org", required=True)
    m.add_argument("--email", required=True)
    m.add_argument("--role", required=True, choices=sorted(ROLE_PERMISSIONS))
    lm = sub.add_parser("list-members", help="Lista los miembros de una organización")
    lm.add_argument("--org", required=True)
    a = p.parse_args(argv)
    if a.cmd != "issue-token":
        return _provision(a)
    try:
        uuid.UUID(a.org)
    except ValueError:
        print("--org debe ser un UUID", file=sys.stderr)
        return 2
    if not 60 <= a.ttl <= 86400:
        print("--ttl debe estar entre 60 y 86400", file=sys.stderr)
        return 2
    print(issue_token(a.user, a.org, a.role, a.ttl))
    return 0


def _provision(a) -> int:
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.services import provisioning as pv

    url = os.environ.get("ADMIN_DATABASE_URL")
    if not url:
        print("Falta ADMIN_DATABASE_URL (conexión con el rol de migraciones, p. ej. postgresql+asyncpg://omni:...@host/omnivoice)",
              file=sys.stderr)
        return 2

    async def run():
        engine = create_async_engine(url)
        try:
            if a.cmd == "create-org":
                return await pv.create_organization(engine, a.name, a.owner_email, a.retention_days)
            if a.cmd == "set-member":
                return await pv.set_member(engine, a.org, a.email, a.role)
            return await pv.list_members(engine, a.org)
        finally:
            await engine.dispose()

    try:
        print(json.dumps(asyncio.run(run()), ensure_ascii=False, indent=2))
    except pv.ProvisioningError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
