"""Alta de organizaciones y usuarios (T2.6).

Decisión: NO se expone por HTTP. La app corre con `omni_app`, que por diseño no puede leer ni escribir
`users`, `memberships` ni `organizations` (migración 002). Dar a la API una conexión privilegiada ampliaría
mucho el daño de cualquier fallo de autorización, así que el aprovisionamiento es una herramienta de operador
(`python -m app.cli ...`) que usa una URL de administración separada (ADMIN_DATABASE_URL).
Las contraseñas no existen aquí: la identidad la emite un proveedor externo (JWT).
"""
import json
import re
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.security.auth import ROLE_PERMISSIONS

EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,}$")


class ProvisioningError(ValueError):
    """Entrada inválida o regla de negocio violada; el mensaje es seguro de mostrar al operador."""


def _email(value: str) -> str:
    e = (value or "").strip().lower()
    if not EMAIL_RE.match(e):
        raise ProvisioningError("Email inválido")
    return e


def _role(value: str) -> str:
    if value not in ROLE_PERMISSIONS:
        raise ProvisioningError(f"Rol inválido; usa uno de {sorted(ROLE_PERMISSIONS)}")
    return value


async def _user_id(c, email: str) -> str:
    row = (await c.execute(
        text("INSERT INTO users (email) VALUES (:e) ON CONFLICT (email) DO UPDATE SET email = EXCLUDED.email RETURNING id"),
        {"e": email})).first()
    return str(row[0])


async def create_organization(engine: AsyncEngine, name: str, owner_email: str, retention_days: int = 30) -> dict:
    name = (name or "").strip()
    if not 2 <= len(name) <= 120:
        raise ProvisioningError("El nombre debe tener entre 2 y 120 caracteres")
    if not 1 <= retention_days <= 3650:
        raise ProvisioningError("retention_days debe estar entre 1 y 3650")
    email = _email(owner_email)
    org_id = str(uuid.uuid4())
    async with engine.begin() as c:
        await c.execute(text("INSERT INTO organizations (id, name, retention_days) VALUES (:i, :n, :d)"),
                        {"i": org_id, "n": name, "d": retention_days})
        uid = await _user_id(c, email)
        await c.execute(text("INSERT INTO memberships (user_id, org_id, role) VALUES (:u, :o, 'owner')"),
                        {"u": uid, "o": org_id})
        await c.execute(text("INSERT INTO audit_logs (org_id, actor, action, detail) VALUES (:o, 'platform', 'org.created', "
                             "CAST(:d AS jsonb))"), {"o": org_id, "d": json.dumps({"owner_user_id": uid})})
    return {"org_id": org_id, "name": name, "owner_user_id": uid, "owner_email": email}


async def set_member(engine: AsyncEngine, org_id: str, email: str, role: str) -> dict:
    """Añade un usuario a la organización o cambia su rol. Nunca deja a la organización sin owner."""
    try:
        uuid.UUID(org_id)
    except ValueError:
        raise ProvisioningError("org_id debe ser un UUID") from None
    email, role = _email(email), _role(role)
    async with engine.begin() as c:
        if not (await c.execute(text("SELECT 1 FROM organizations WHERE id = :o FOR UPDATE"), {"o": org_id})).first():
            raise ProvisioningError("La organización no existe")
        uid = await _user_id(c, email)
        current = (await c.execute(text("SELECT role FROM memberships WHERE user_id = :u AND org_id = :o"),
                                   {"u": uid, "o": org_id})).scalar()
        if current == "owner" and role != "owner":
            owners = (await c.execute(text("SELECT count(*) FROM memberships WHERE org_id = :o AND role = 'owner'"),
                                      {"o": org_id})).scalar()
            if owners <= 1:
                raise ProvisioningError("No se puede degradar al último owner de la organización")
        await c.execute(
            text("INSERT INTO memberships (user_id, org_id, role) VALUES (:u, :o, :r) "
                 "ON CONFLICT (user_id, org_id) DO UPDATE SET role = EXCLUDED.role"),
            {"u": uid, "o": org_id, "r": role})
        await c.execute(text("INSERT INTO audit_logs (org_id, actor, action, detail) VALUES (:o, 'platform', 'member.set', "
                             "CAST(:d AS jsonb))"),
                        {"o": org_id, "d": json.dumps({"user_id": uid, "role": role, "previous": current})})
    return {"org_id": org_id, "user_id": uid, "email": email, "role": role, "previous_role": current}


async def set_plan(engine: AsyncEngine, org_id: str, plan: str) -> dict:
    from app.billing.plans import PLAN_IDS

    try:
        uuid.UUID(org_id)
    except ValueError:
        raise ProvisioningError("org_id debe ser un UUID") from None
    if plan not in PLAN_IDS:
        raise ProvisioningError(f"Plan inválido; usa uno de {list(PLAN_IDS)}")
    async with engine.begin() as c:
        row = (await c.execute(text("UPDATE organizations SET plan = :p WHERE id = :o RETURNING plan"), {"p": plan, "o": org_id})).first()
        if not row:
            raise ProvisioningError("La organización no existe")
        await c.execute(text("INSERT INTO audit_logs (org_id, actor, action, detail) VALUES (:o, 'platform', 'org.plan_changed', CAST(:d AS jsonb))"),
                        {"o": org_id, "d": json.dumps({"plan": plan})})
    return {"org_id": org_id, "plan": plan}


async def list_members(engine: AsyncEngine, org_id: str) -> list[dict]:
    try:
        uuid.UUID(org_id)
    except ValueError:
        raise ProvisioningError("org_id debe ser un UUID") from None
    async with engine.begin() as c:
        rows = (await c.execute(
            text("SELECT u.id, u.email, m.role FROM memberships m JOIN users u ON u.id = m.user_id "
                 "WHERE m.org_id = :o ORDER BY u.email"), {"o": org_id})).all()
    return [{"user_id": str(r[0]), "email": r[1], "role": r[2]} for r in rows]
