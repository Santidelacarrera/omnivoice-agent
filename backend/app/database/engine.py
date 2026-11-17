"""Motor async de SQLAlchemy con aislamiento por organización.

Cada operación abre una transacción y fija `app.org_id` con set_config(..., is_local=true),
de modo que las políticas RLS de PostgreSQL filtran por organización aunque el código
olvide una cláusula WHERE. La app debe conectarse con un rol NO superusuario
(ver migrations/002_app_role.sql): los superusuarios y propietarios omiten RLS.
"""
from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

_engine: AsyncEngine | None = None


def init_engine(database_url: str, connect_timeout_s: float = 5.0, command_timeout_s: float = 15.0) -> AsyncEngine:
    """Motor con plazos explícitos: sin ellos, una base de datos que no responde (paquetes descartados) bloquearía cada operación hasta 60 s."""
    global _engine
    _engine = create_async_engine(database_url, pool_size=10, max_overflow=10, pool_pre_ping=True, pool_timeout=connect_timeout_s,
                                  connect_args={"timeout": connect_timeout_s, "command_timeout": command_timeout_s})
    return _engine


def get_engine() -> AsyncEngine:
    if _engine is None:
        raise RuntimeError("Base de datos no inicializada")
    return _engine


async def dispose_engine() -> None:
    global _engine
    if _engine is not None:
        await _engine.dispose()
        _engine = None


@asynccontextmanager
async def org_transaction(org_id: str) -> AsyncIterator[AsyncConnection]:
    """Transacción con RLS activo para `org_id`. Lanza si org_id no es un UUID válido."""
    async with get_engine().begin() as conn:
        await conn.execute(text("SELECT set_config('app.org_id', :org, true)"), {"org": org_id})
        yield conn
