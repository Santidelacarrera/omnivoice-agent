import time
import uuid
from dataclasses import dataclass

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import get_settings
from app.security import apikeys

bearer = HTTPBearer(auto_error=False)

_READ = {"agents:read", "conversations:read", "metrics:read", "calls:read"}
_FULL = {*_READ, "conversations:delete", "agents:write", "agents:publish", "audit:read", "session:create",
         "recordings:read", "telephony:call", "phones:read", "phones:write", "outbound:write", "apikeys:write"}
ROLE_PERMISSIONS: dict[str, set[str]] = {
    "owner": set(_FULL),
    "admin": set(_FULL),
    "manager": {*_READ, "agents:write", "agents:publish", "session:create", "telephony:call", "phones:read", "audit:read",
                "outbound:write"},
    "operator": {*_READ, "session:create", "telephony:call", "phones:read"},
    "analyst": {*_READ, "phones:read", "audit:read"},
    "viewer": set(_READ),
    "customer": {"session:create"},
}


@dataclass(frozen=True)
class Principal:
    user_id: str
    org_id: str
    role: str

    def can(self, permission: str) -> bool:
        return permission in ROLE_PERMISSIONS.get(self.role, set())


def issue_token(user_id: str, org_id: str, role: str, ttl: int | None = None) -> str:
    s = get_settings()
    now = int(time.time())
    payload = {"sub": user_id, "org": org_id, "role": role, "iat": now, "exp": now + (ttl or s.jwt_ttl_seconds)}
    return jwt.encode(payload, s.jwt_secret, algorithm=s.jwt_algorithm)


def decode_token(token: str) -> Principal:
    s = get_settings()
    try:
        data = jwt.decode(token, s.jwt_secret, algorithms=[s.jwt_algorithm],
                          options={"require": ["exp", "sub", "org", "role"]})
    except jwt.PyJWTError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token inválido o expirado") from exc
    if not all(isinstance(data.get(k), str) and data[k] for k in ("sub", "org", "role")) or data["role"] not in ROLE_PERMISSIONS:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token inválido o expirado")
    if s.persistence_backend == "postgres":
        try:
            uuid.UUID(str(data["org"]))  # RLS compara contra uuid: rechazamos antes de llegar a la BD
        except ValueError as exc:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Organización inválida") from exc
    return Principal(user_id=data["sub"], org_id=data["org"], role=data["role"])


async def principal_from_api_key(request: Request, token: str) -> Principal:
    bad = HTTPException(status.HTTP_401_UNAUTHORIZED, "Credencial inválida")  # mismo mensaje para cualquier causa
    prefix = apikeys.parse_prefix(token)
    if prefix is None:
        raise bad
    db = request.app.state.db
    row = await db.resolve_api_key(prefix)
    if not row or not apikeys.verify(token, row["key_hash"], row["expires_at"], row["revoked"]):
        raise bad
    if row["role"] not in ROLE_PERMISSIONS or row["role"] == "owner":
        raise bad
    try:  # marca de último uso, como mucho una vez por minuto y nunca rompe la petición
        if await request.app.state.store.hit("apikey-touch", row["id"], 1, 60):
            await db.touch_api_key(row["org_id"], row["id"])
    except Exception:  # noqa: BLE001
        pass
    return Principal(user_id=f"apikey:{row['id']}", org_id=row["org_id"], role=row["role"])


async def current_principal(request: Request, creds: HTTPAuthorizationCredentials | None = Depends(bearer)) -> Principal:
    if creds is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Falta credencial")
    if creds.credentials.startswith(apikeys.KEY_PREFIX):
        return await principal_from_api_key(request, creds.credentials)
    return decode_token(creds.credentials)


def require(permission: str):
    async def dep(p: Principal = Depends(current_principal)) -> Principal:
        if not p.can(permission):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Permiso insuficiente")
        return p

    return dep
