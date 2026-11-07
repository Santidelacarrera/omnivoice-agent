"""Pruebas contra PostgreSQL y Redis REALES (el camino de producción).

Se omiten si faltan TEST_DATABASE_URL / TEST_REDIS_URL; el CI las ejecuta con servicios reales.
TEST_DATABASE_URL debe usar el rol `omni_app` (sin superusuario) sobre una base con las migraciones aplicadas.
"""
import os
import time
import uuid

import pytest
from sqlalchemy import text

from app.security.auth import Principal

PG_URL = os.environ.get("TEST_DATABASE_URL")
REDIS_URL = os.environ.get("TEST_REDIS_URL")
DEMO = "00000000-0000-0000-0000-000000000001"
OTHER = "00000000-0000-0000-0000-0000000000ff"

needs_pg = pytest.mark.skipif(not PG_URL, reason="requiere TEST_DATABASE_URL")
needs_redis = pytest.mark.skipif(not REDIS_URL, reason="requiere TEST_REDIS_URL")


@pytest.fixture
async def pg():
    from app.database.engine import dispose_engine, init_engine
    from app.services.pg_persistence import PostgresPersistence

    init_engine(PG_URL)
    yield PostgresPersistence()
    await dispose_engine()


@pytest.fixture
async def store():
    import redis.asyncio as aioredis

    from app.services.state_store import RedisStateStore

    client = aioredis.from_url(REDIS_URL, decode_responses=True)
    await client.flushdb()
    yield RedisStateStore(client)
    await client.flushdb()
    await client.aclose()


# ---------------- PostgreSQL ----------------

@needs_pg
async def test_app_role_is_not_privileged(pg):
    from app.database.engine import get_engine

    async with get_engine().connect() as c:
        row = (await c.execute(text("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user"))).first()
    assert row[0] is False


@needs_pg
async def test_conversation_lifecycle_and_erasure(pg):
    cid = await pg.start_conversation(DEMO, "s1", "u1")
    await pg.add_transcript(DEMO, cid, "user", "hola")
    await pg.add_transcript(DEMO, cid, "agent", "buenas")
    await pg.add_event(DEMO, cid, "barge_in", {"x": 1}, "corr")
    await pg.record_tool(DEMO, cid, {"tool": "check_inventory", "call_id": uuid.uuid4().hex, "args": {"keys": ["product"]},
                                      "result": {"ok": True}, "ok": True, "duration_ms": 5})
    await pg.end_conversation(DEMO, cid, {"final_state": "COMPLETED", "first_audio_ms": 300, "audio_seconds": 12})
    await pg.add_recording(DEMO, cid, f"{DEMO}/{cid}.wav", 1000, 1.5)

    detail = await pg.get_conversation(DEMO, cid)
    assert [t["speaker"] for t in detail["transcript"]] == ["user", "agent"]
    assert detail["final_state"] == "COMPLETED" and len(detail["tools"]) == 1
    assert (await pg.get_recording(DEMO, cid))["storage_key"].endswith(".wav")
    assert any(c["id"] == cid for c in await pg.list_conversations(DEMO))
    assert (await pg.usage_summary(DEMO))["audio_seconds"] >= 12

    # otra organización no ve ni borra nada (RLS)
    assert await pg.get_conversation(OTHER, cid) is None
    assert await pg.delete_conversation(OTHER, cid) is None

    keys = await pg.delete_conversation(DEMO, cid)
    assert keys == [f"{DEMO}/{cid}.wav"]
    assert await pg.get_conversation(DEMO, cid) is None
    assert await pg.get_recording(DEMO, cid) is None


@needs_pg
async def test_audit_agents_and_rls_isolation(pg):
    await pg.audit(DEMO, "tester", "test.action", {"k": "v"})
    assert any(a["action"] == "test.action" for a in await pg.list_audit(DEMO))
    assert not any(a["action"] == "test.action" for a in await pg.list_audit(OTHER))

    agent = await pg.create_agent(DEMO, "Agente prueba", "Instrucciones de prueba largas", ["check_inventory"], None, "es")
    assert (await pg.get_agent(DEMO, agent["id"]))["tools"] == ["check_inventory"]
    assert await pg.get_agent(OTHER, agent["id"]) is None


@needs_pg
async def test_business_repository_reads_seed_data_per_org():
    from app.database.engine import dispose_engine, init_engine
    from app.database.repository import PostgresRepository

    init_engine(PG_URL)
    try:
        repo = PostgresRepository()
        inv = await repo.inventory(DEMO, "chaqueta", "negro", "M")
        assert inv["available"] and inv["units"] == 4
        assert (await repo.inventory(OTHER, "chaqueta", "negro", "M"))["available"] is False
        res = await repo.create_reservation(DEMO, {"name": "Ana", "date": "2030-01-02", "time": "19:30", "party_size": 2})
        assert (await repo.get_reservation(DEMO, res["reservation_id"]))["party_size"] == 2
        assert await repo.get_reservation(OTHER, res["reservation_id"]) is None
        assert await repo.search_kb(DEMO, "devoluciones")
        assert await repo.search_kb(OTHER, "devoluciones") == []
    finally:
        await dispose_engine()


@needs_pg
async def test_retention_backend_purges_old_conversations(pg):
    from app.services.retention_pg import PostgresRetentionBackend

    cid = await pg.start_conversation(DEMO, "s2", "u1")
    await pg.add_transcript(DEMO, cid, "user", "para purgar")
    backend = PostgresRetentionBackend()
    assert any(o == DEMO for o, _ in await backend.list_orgs())
    expired = await backend.expired_conversations(DEMO, time.time() + 3600, 1000)  # «todo es anterior a dentro de 1 h»
    assert any(c == cid for c, _ in expired)
    assert await backend.delete_conversations(DEMO, [cid]) >= 1
    assert await pg.get_conversation(DEMO, cid) is None


# ---------------- Redis ----------------

@needs_redis
async def test_tickets_are_single_use(store):
    p = Principal("u1", "o1", "admin")
    await store.put_ticket("t1", p, 30, "agent-1")
    assert await store.take_ticket("t1") == (p, "agent-1")
    assert await store.take_ticket("t1") is None


@needs_redis
async def test_rate_limit_window(store):
    results = [await store.hit("api", "o1:u1", 3, 60) for _ in range(5)]
    assert results == [True, True, True, False, False]


@needs_redis
async def test_session_quota_is_atomic_and_releasable(store):
    import asyncio

    got = await asyncio.gather(*(store.acquire_session("o1", f"s{i}", 3) for i in range(10)))
    assert sum(got) == 3
    assert await store.active_sessions("o1") == 3
    await store.release_session("o1", "s0")
    assert await store.active_sessions("o1") == 2


@needs_redis
async def test_options_lock_and_usage(store):
    await store.put_session_options("k", {"voice": "alloy"}, 30)
    assert await store.take_session_options("k") == {"voice": "alloy"}
    assert await store.take_session_options("k") == {}
    assert await store.acquire_lock("retention", 30) is True
    assert await store.acquire_lock("retention", 30) is False
    await store.add_usage("o1", 12.5)
    await store.add_usage("o1", 2.5)
    assert await store.usage_seconds("o1") == 15.0
