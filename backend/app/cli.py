"""Utilidades de operador. Uso:  python -m app.cli issue-token --org <uuid> --user <id> --role admin [--ttl 3600]

Sirve para emitir tokens mientras no hay un proveedor de identidad externo (p. ej. un administrador o un servicio
de confianza). Usa el mismo JWT_SECRET que el backend, así que ejecútalo dentro del contenedor:
  docker compose exec backend python -m app.cli issue-token --org <uuid> --user admin1 --role admin
"""
import argparse
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
    a = p.parse_args(argv)
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


if __name__ == "__main__":
    raise SystemExit(main())
