"""Cifrado de secretos en reposo (p. ej. el secreto de firma de un webhook, que hay que poder leer para firmar).

Fernet (AES-128-CBC + HMAC-SHA256) con una clave de `WEBHOOK_SECRET_KEY`. Si no se define, se deriva de `JWT_SECRET`: sirve para
empezar, pero rotar `JWT_SECRET` dejaría ilegibles los secretos guardados; en producción define una clave propia.
La base de datos solo guarda el texto cifrado: un volcado de la base no revela los secretos sin la clave de la aplicación.
"""
import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken


class SecretBoxError(Exception):
    """El secreto cifrado no se pudo leer (clave distinta o dato corrupto)."""


def _fernet(key_material: str) -> Fernet:
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(("omnivoice-secretbox:" + key_material).encode()).digest()))


def encrypt(key_material: str, plaintext: str) -> str:
    return _fernet(key_material).encrypt(plaintext.encode()).decode()


def decrypt(key_material: str, token: str) -> str:
    try:
        return _fernet(key_material).decrypt(token.encode()).decode()
    except (InvalidToken, ValueError):
        raise SecretBoxError("No se pudo descifrar el secreto") from None
