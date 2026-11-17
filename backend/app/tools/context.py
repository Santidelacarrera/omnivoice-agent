"""Contexto de la llamada visible solo para las herramientas.

Las herramientas reciben (argumentos, principal). Algunas necesitan algo más de la llamada que el modelo NO debe poder elegir:
a qué número se envía una confirmación (siempre el de quien llama) o desde qué línea. Ese dato vive en una variable de contexto
fijada por la sesión alrededor de cada ejecución; el modelo nunca lo ve ni lo pasa como argumento, así que no puede redirigirlo.
El número completo solo existe en la memoria de la sesión: no se guarda en la base de datos ni en el registro de auditoría.
"""
from contextvars import ContextVar
from typing import Any

CALL_CONTEXT: ContextVar[dict[str, Any] | None] = ContextVar("omnivoice_call_context", default=None)


def caller_hash(principal_user_id: str) -> str | None:
    """Huella del número de quien llama (la misma de `calls.peer_hash`) si la sesión es telefónica; None si no está identificado."""
    return principal_user_id.split(":", 1)[1] if principal_user_id.startswith("phone:") else None
