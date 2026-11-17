"""Claves de API (`ovk_<prefijo>_<secreto>`). Se guarda solo el hash SHA-256; la clave completa se muestra una vez.

El secreto tiene 256 bits de entropía, así que un hash rápido es adecuado (no es una contraseña elegida por una persona).
La comparación es en tiempo constante. Una clave revocada, vencida, alterada o inexistente produce el mismo 401 genérico.
"""
import hashlib
import hmac
import secrets
import time

KEY_PREFIX = "ovk_"
PREFIX_LEN = 8
ASSIGNABLE_ROLES = ("admin", "manager", "operator", "analyst", "viewer")  # nunca owner


def generate() -> tuple[str, str, str]:
    """(clave completa, prefijo, hash). La clave completa no se vuelve a poder obtener."""
    prefix = secrets.token_hex(PREFIX_LEN // 2)
    key = f"{KEY_PREFIX}{prefix}_{secrets.token_urlsafe(32)}"
    return key, prefix, hash_key(key)


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def parse_prefix(token: str) -> str | None:
    """Extrae el prefijo de `ovk_<prefijo>_<secreto>`; None si el formato no es el esperado."""
    if not token.startswith(KEY_PREFIX) or len(token) > 120:
        return None
    rest = token[len(KEY_PREFIX):]
    prefix, sep, secret = rest.partition("_")
    if sep != "_" or len(prefix) != PREFIX_LEN or not secret or any(c not in "0123456789abcdef" for c in prefix):
        return None
    return prefix


def verify(token: str, stored_hash: str, expires_at: float | None, revoked: bool, now: float | None = None) -> bool:
    if revoked or not hmac.compare_digest(hash_key(token), stored_hash):
        return False
    return expires_at is None or expires_at > (now or time.time())
