import asyncio
import struct

import pytest

from app.core.config import Settings
from app.observability.metrics import LatencyWindow
from app.orchestration.session import VoiceSession
from app.realtime.provider import FakeProvider
from app.security.auth import Principal
from app.services.persistence import InMemoryPersistence
from app.services.state_store import InMemoryStateStore
from app.tools.handlers import InMemoryRepository, build_registry

P = Principal("u1", "o1", "customer")
LOUD = struct.pack("<480h", *([12000, -12000] * 240))


async def test_ticket_is_single_use_and_carries_agent():
    st = InMemoryStateStore()
    await st.put_ticket("t1", P, 30, "agent-9")
    assert await st.take_ticket("t1") == (P, "agent-9")
    assert await st.take_ticket("t1") is None  # no se puede reutilizar


async def test_ticket_expires():
    st = InMemoryStateStore()
    await st.put_ticket("t1", P, -1)
    assert await st.take_ticket("t1") is None


async def test_rate_limit_blocks_after_limit_and_is_per_identity():
    st = InMemoryStateStore()
    results = [await st.hit("api", "a", 3, 60) for _ in range(5)]
    assert results == [True, True, True, False, False]
    assert await st.hit("api", "b", 3, 60) is True


async def test_session_cap_is_enforced_and_released():
    st = InMemoryStateStore()
    assert await st.acquire_session("o1", "s1", 2)
    assert await st.acquire_session("o1", "s2", 2)
    assert not await st.acquire_session("o1", "s3", 2)
    assert await st.acquire_session("o2", "s1", 2)  # otra organización: cupo independiente
    await st.release_session("o1", "s1")
    assert await st.acquire_session("o1", "s3", 2)
    assert await st.active_sessions("o1") == 2


async def test_stale_sessions_are_reclaimed():
    st = InMemoryStateStore()
    assert await st.acquire_session("o1", "dead", 1)
    st._sessions["o1"]["dead"] -= 10_000  # simula un proceso caído que no liberó el cupo
    assert await st.acquire_session("o1", "alive", 1, stale_after_s=7200)


def test_latency_window_nearest_rank():
    w = LatencyWindow()
    for i in range(1, 101):
        w.add(i / 1000)
    p = w.percentiles(scale=1000)
    assert (p["count"], p["p50"], p["p95"], p["p99"]) == (100, 50.0, 95.0, 99.0)
    assert LatencyWindow().percentiles()["p50"] is None


def test_production_config_rejects_insecure_values():
    base = dict(environment="production", persistence_backend="postgres", state_backend="redis",
                jwt_secret="x" * 40, cors_origins=["https://app.example.com"])
    Settings(**base)  # válida
    for bad in (dict(jwt_secret="change-me"), dict(jwt_secret="corta"), dict(persistence_backend="memory"),
                dict(state_backend="memory"), dict(cors_origins=["*"])):
        with pytest.raises(ValueError):
            Settings(**{**base, **bad})
    Settings(environment="development")  # desarrollo no exige nada


async def test_session_persists_transcripts_audit_tools_and_usage():
    db = InMemoryPersistence()
    script = [
        {"type": "transcript_user", "text": "¿Hay chaqueta negra M?"},
        {"type": "tool_call", "call_id": "k1", "name": "check_inventory",
         "arguments": '{"product":"chaqueta","color":"negro","size":"M"}'},
        {"type": "transcript_agent", "text": "Sí, quedan 4."},
    ]
    prov = FakeProvider(script=script, audio_chunks=0)
    sent = []

    async def send(m):
        sent.append(m)

    s = VoiceSession(P, prov, build_registry(InMemoryRepository()), send, Settings(sample_rate=24000), persistence=db)
    await s.start()
    for seq in range(50):  # 50 frames de 20 ms = 1 s de audio entrante
        await s.on_audio(LOUD, seq)
    prov.trigger_response()
    await asyncio.sleep(0.2)
    await s.close()

    conv = db.convs[s.conversation_id]
    assert conv["final_state"] == "COMPLETED" and conv["audio_seconds"] >= 0.9
    assert [t["speaker"] for t in db.transcripts[s.conversation_id]] == ["user", "agent"]
    assert db.tools and db.tools[0]["tool"] == "check_inventory"
    assert "args" in db.tools[0] and "chaqueta" not in str(db.tools[0]["args"])  # sin datos de usuario en claro
    actions = [a["action"] for a in db.audits]
    assert actions[0] == "session.started" and "tool.executed" in actions and actions[-1] == "session.completed"
    assert len({a["detail"]["correlation_id"] for a in db.audits}) == 1  # un id para toda la sesión
    assert (await db.usage_summary("o1"))["audio_seconds"] >= 0.9


async def test_conversations_are_isolated_between_orgs():
    db = InMemoryPersistence()
    cid = await db.start_conversation("o1", "s", "u1")
    assert await db.get_conversation("o1", cid) is not None
    assert await db.get_conversation("o2", cid) is None
    assert await db.list_conversations("o2") == []
