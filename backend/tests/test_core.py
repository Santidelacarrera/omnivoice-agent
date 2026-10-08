import asyncio
import struct

import pytest

from app.core.config import Settings
from app.observability.metrics import SessionMetrics
from app.orchestration.session import VoiceSession
from app.orchestration.state_machine import InvalidTransition, SessionStateMachine, State
from app.realtime.provider import FakeProvider
from app.realtime.vad import VoiceActivityDetector
from app.security.auth import Principal, decode_token, issue_token
from app.tools.handlers import InMemoryRepository, build_registry

P = Principal("u1", "o1", "customer")
LOUD = struct.pack("<480h", *([12000, -12000] * 240))
QUIET = b"\x00\x00" * 480


def make_session(provider=None):
    sent = []

    async def send(m):
        sent.append(m)

    prov = provider or FakeProvider()
    s = VoiceSession(P, prov, build_registry(InMemoryRepository()), send, Settings(sample_rate=24000, vad_backend="energy"))
    return s, prov, sent


def test_state_machine_rejects_invalid():
    sm = SessionStateMachine()
    with pytest.raises(InvalidTransition):
        sm.to(State.RESPONDING)
    sm.to(State.LISTENING)
    sm.to(State.PROCESSING)
    sm.to(State.RESPONDING)


def test_jwt_roundtrip_and_expiry():
    tok = issue_token("u", "o", "admin")
    assert decode_token(tok).org_id == "o"
    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        decode_token(issue_token("u", "o", "admin", ttl=-5))


def test_vad_detects_start_and_end():
    v = VoiceActivityDetector(0.015, 40, 24000, hangover_ms=100)
    events = [v.feed(LOUD) for _ in range(5)] + [v.feed(QUIET) for _ in range(12)]
    assert "speech_start" in events and "speech_end" in events


def test_packet_loss_tracking():
    m = SessionMetrics("s", "o")
    for seq in (1, 2, 5):
        m.track_seq(seq)
    assert m.lost_packets == 2


@pytest.mark.asyncio
async def test_tool_validation_permissions_and_tenancy():
    reg = build_registry(InMemoryRepository())
    bad = await reg.execute("check_inventory", {"product": "x", "oops": 1}, P, "c1")
    assert bad["error"] == "argumentos_invalidos"
    assert (await reg.execute("rm_rf", {}, P, "c2"))["error"] == "herramienta_no_disponible"
    ok = await reg.execute("check_inventory", {"product": "chaqueta", "color": "negro", "size": "M"}, P, "c3")
    assert ok["ok"] and ok["data"]["available"]
    other_org = Principal("u2", "o2", "customer")
    leak = await reg.execute("check_inventory", {"product": "chaqueta", "color": "negro", "size": "M"}, other_org, "c4")
    assert not leak["data"]["available"]


@pytest.mark.asyncio
async def test_mutating_tools_are_idempotent():
    repo = InMemoryRepository()
    reg = build_registry(repo)
    args = {"subject": "No funciona", "description": "Error 500 al pagar"}
    a = await reg.execute("create_support_ticket", args, P, "same-call")
    b = await reg.execute("create_support_ticket", args, P, "same-call")
    assert a == b and len(repo.tickets) == 1


@pytest.mark.asyncio
async def test_barge_in_clears_audio_and_cancels_provider():
    s, prov, sent = make_session()
    await s.start()
    prov.trigger_response()
    await s.on_audio(QUIET)  # mantiene sesión viva
    await asyncio.sleep(0.1)
    assert s.sm.state == State.RESPONDING
    await s.on_client_event({"type": "barge_in"})
    assert prov.cancelled == 1
    assert {"type": "audio.clear"} in sent
    assert s.sm.state == State.LISTENING
    assert s.metrics.interruptions == 1
    n_audio = sum(isinstance(m, bytes) for m in sent)
    await asyncio.sleep(0.2)
    assert sum(isinstance(m, bytes) for m in sent) == n_audio  # no llega audio obsoleto
    await s.close()


@pytest.mark.asyncio
async def test_tool_call_flow_returns_real_result_to_model():
    script = [
        {"type": "tool_call", "call_id": "k1", "name": "check_inventory",
         "arguments": '{"product":"chaqueta","color":"negro","size":"M"}'},
    ]
    prov = FakeProvider(script=script, audio_chunks=0)
    s, prov, sent = make_session(prov)
    await s.start()
    prov.trigger_response()
    await asyncio.sleep(0.2)
    assert prov.tool_results and prov.tool_results[0][1]["data"]["available"] is True
    await s.close()
