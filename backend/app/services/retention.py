"""Purga automática por `organizations.retention_days`.

Cada organización decide cuántos días se conservan sus conversaciones (transcripciones, eventos, herramientas y
grabaciones). El job borra lo vencido en lotes y registra cuánto purgó en la auditoría (que es append-only y no se
purga: no contiene contenido de la conversación).

Garantías:
* Los objetos de grabación se borran ANTES que las filas. Si el almacenamiento falla, la conversación se conserva
  y se reintenta en la siguiente ejecución: nunca quedan archivos huérfanos sin referencia.
* Con varias réplicas, un candado (Redis) evita que dos instancias purguen a la vez.
"""
import asyncio
import time
from typing import Any, Awaitable, Callable, Protocol

import structlog

from app.observability.metrics import RETENTION_DELETED, RETENTION_RUNS

log = structlog.get_logger()

DAY = 86400


class RetentionBackend(Protocol):
    async def list_orgs(self) -> list[tuple[str, int]]: ...
    async def expired_conversations(self, org_id: str, cutoff_ts: float, limit: int) -> list[tuple[str, list[str]]]: ...
    async def delete_conversations(self, org_id: str, conversation_ids: list[str]) -> int: ...


Audit = Callable[[str, str | None, str, dict[str, Any]], Awaitable[None]]


async def purge_once(backend: RetentionBackend, storage: Any, audit: Audit, now: float | None = None,
                     batch_size: int = 500) -> dict[str, dict[str, int]]:
    """Una pasada completa. Devuelve estadísticas por organización: conversaciones, grabaciones, omitidas."""
    now = time.time() if now is None else now
    result: dict[str, dict[str, int]] = {}
    for org_id, days in await backend.list_orgs():
        if days is None or days <= 0:
            continue  # 0 o negativo = conservar sin límite
        cutoff = now - days * DAY
        stats = {"conversations": 0, "recordings": 0, "skipped": 0}
        while True:
            batch = await backend.expired_conversations(org_id, cutoff, batch_size)
            if not batch:
                break
            deletable: list[str] = []
            for conv_id, keys in batch:
                if keys and storage is None:
                    stats["skipped"] += 1  # hay audio pero no hay almacenamiento configurado: no se pierde la referencia
                    continue
                try:
                    for key in keys:
                        await storage.delete(key)
                except Exception:  # noqa: BLE001
                    stats["skipped"] += 1
                    log.exception("retention_storage_delete_failed", org=org_id, conversation=conv_id)
                    continue
                stats["recordings"] += len(keys)
                deletable.append(conv_id)
            if not deletable:
                break  # nada avanzó (todo omitido): se reintenta en la próxima pasada
            await backend.delete_conversations(org_id, deletable)
            stats["conversations"] += len(deletable)
            if len(batch) < batch_size:
                break
        if stats["conversations"] or stats["skipped"]:
            RETENTION_DELETED.labels("conversations").inc(stats["conversations"])
            RETENTION_DELETED.labels("recordings").inc(stats["recordings"])
            await audit(org_id, "system:retention", "retention.purged", {**stats, "retention_days": days})
            result[org_id] = stats
    RETENTION_RUNS.inc()
    return result


class RetentionJob:
    def __init__(self, backend: RetentionBackend, storage: Any, audit: Audit,
                 acquire_lock: Callable[[str, int], Awaitable[bool]], interval_s: int, batch_size: int,
                 initial_delay_s: float = 30.0) -> None:
        self.backend, self.storage, self.audit = backend, storage, audit
        self.acquire_lock, self.interval_s, self.batch_size = acquire_lock, interval_s, batch_size
        self.initial_delay_s = initial_delay_s
        self._task: asyncio.Task | None = None

    async def run_once(self) -> dict[str, dict[str, int]] | None:
        # TTL algo menor que el intervalo: si una réplica muere con el candado, otra retoma en la siguiente vuelta.
        if not await self.acquire_lock("retention", max(60, self.interval_s - 30)):
            return None
        try:
            return await purge_once(self.backend, self.storage, self.audit, batch_size=self.batch_size)
        except Exception:  # noqa: BLE001 - el job no debe morir por un fallo puntual
            log.exception("retention_run_failed")
            return None

    async def _loop(self) -> None:
        await asyncio.sleep(self.initial_delay_s)
        while True:
            await self.run_once()
            await asyncio.sleep(self.interval_s)

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)


class InMemoryRetentionBackend:
    """Para desarrollo y tests con `InMemoryPersistence`. Todas las organizaciones usan `default_days`."""

    def __init__(self, persistence: Any, default_days: int = 30, days_by_org: dict[str, int] | None = None) -> None:
        self.p, self.default_days, self.days_by_org = persistence, default_days, days_by_org or {}

    async def list_orgs(self):
        orgs = {c["org_id"] for c in self.p.convs.values()} | set(self.days_by_org)
        return [(o, self.days_by_org.get(o, self.default_days)) for o in sorted(orgs)]

    async def expired_conversations(self, org_id, cutoff_ts, limit):
        old = sorted((c for c in self.p.convs.values() if c["org_id"] == org_id and c["created_at"] < cutoff_ts),
                     key=lambda c: c["created_at"])[:limit]
        return [(c["id"], [r["storage_key"] for r in self.p.recordings.get(c["id"], [])]) for c in old]

    async def delete_conversations(self, org_id, conversation_ids):
        n = 0
        for cid in conversation_ids:
            if self.p.convs.get(cid, {}).get("org_id") == org_id:
                for store in (self.p.convs, self.p.transcripts, self.p.events, self.p.recordings):
                    store.pop(cid, None)
                self.p.tools = [t for t in self.p.tools if t.get("conversation_id") != cid]
                n += 1
        return n
