import time
import uuid
from dataclasses import dataclass

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import get_settings

bearer = HTTPBearer(auto_error=False)

ROLE_PERMISSIONS: dict[str, set[str]] = {
    "admin": {"agents:write", "agents:read", "conversations:read", "metrics:read", "audit:read", "session:create"},
    "operator": {"agents:read", "conversations:read", "metrics:read", "session:create"},
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
        data = jwt.decode(token, s.jwt_secret, algorithms=[s.jwt_algorithm])
    except jwt.PyJWTError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token inválido o expirado") from exc
    if s.persistence_backend == "postgres":
        try:
            uuid.UUID(str(data["org"]))  # RLS compara contra uuid: rechazamos antes de llegar a la BD
        except ValueError as exc:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Organización inválida") from exc
    return Principal(user_id=data["sub"], org_id=data["org"], role=data["role"])


async def current_principal(creds: HTTPAuthorizationCredentials | None = Depends(bearer)) -> Principal:
    if creds is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Falta credencial")
    return decode_token(creds.credentials)


def require(permission: str):
    async def dep(p: Principal = Depends(current_principal)) -> Principal:
        if not p.can(permission):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Permiso insuficiente")
        return p

    return dep
