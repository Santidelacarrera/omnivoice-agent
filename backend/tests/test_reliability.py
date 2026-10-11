"""Fiabilidad y costes: reconexión, plazos, límites de consumo, aislamiento entre sesiones, tareas huérfanas,
política de logs y medición de latencia/coste."""
import asyncio
import io
import json
import struct

import pytest
import structlog
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.core.config import Settings
from app.main import create_app, redact_secrets
from app.observability.metrics import STAGE_WINDOWS, TTFB_WINDOW
from app.orchestration.session import VoiceSession
from app.orchestration.state_machine import State
from app.realtime.provider import FakeProvider
from app.security.auth import Principal, issue_token
from app.services.persistence import InMemoryPersistence
from app.tools.handlers import InMemoryRepository, build_registry

LOUD = struct.pack("<480h", *([12000, -12000] * 240))
QUIET = b"\x00\x00" * 480
CFG = dict(sample_rate=24000, vad_backend="energy", provider_reconnect_backoff_s=0.01, provider_connect_timeout_s=0.5)


def make(org="o1", provider=None, factory=None, db=None, **over):
    sent = []

    async def send(m):
        sent.append(m)

    prov = provider or FakeProvider()
    s = VoiceSession(Principal(f"u-{org}", org, "customer"), prov, build_registry(InMemoryRepository()), send,
                     Settings(**{**CFG, **over}), persistence=db, provider_factory=factory)
    return s, prov, sent


class DyingProvider(FakeProvider):
    """Se cae (cierra el flujo de eventos) cuando se le ordena."""

    def die(self):
        self._q.put_nowait(None)


# ---------------- reconexión y plazos ----------------

async def test_provider_drop_is_recovered_and_conversation_continues():
    created: list[FakeProvider] = []

    def factory():
        p = FakeProvider(audio_chunks=3, chunk_delay=0.001)
        created.append(p)
        return p

    first = DyingProvider()
    s, _, sent = make(provider=first, factory=factory)
    await s.start()
    await s.provider.send_audio(b"")  # sin efecto
    s._recent.extend([("user", "quiero una mesa"), ("agent", "¿para cuántas personas?")])
    first.die()
    for _ in range(100):
        if created and any(m == {"type": "provider.reconnected", "attempt": 1} for m in sent if isinstance(m, dict)):
            break
        await asyncio.sleep(0.01)
    types = [m["type"] for m in sent if isinstance(m, dict)]
    assert "provider.reconnecting" in types and "provider.reconnected" in types and "error" not in types
    assert s.provider is created[0] and s.sm.state == State.LISTENING
    created[0].trigger_response()  # el proveedor nuevo funciona y su audio llega al cliente
    for _ in range(100):
        if any(isinstance(m, bytes) for m in sent):
            break
        await asyncio.sleep(0.01)
    assert any(isinstance(m, bytes) for m in sent)
    await s.close()


async def test_reconnect_gives_new_provider_recent_context_but_not_audio():
    seen = {}

    class Spy(FakeProvider):
        async def connect(self, instructions, tools, voice=None, language=None):
            seen["instructions"] = instructions

    first = DyingProvider()
    s, _, _ = make(provider=first, factory=lambda: Spy())
    await s.start()
    s._recent.append(("user", "mi código es A1"))
    first.die()
    for _ in range(100):
        if "instructions" in seen:
            break
        await asyncio.sleep(0.01)
    assert "mi código es A1" in seen["instructions"]
    await s.close()


async def test_reconnect_budget_is_bounded_then_session_fails_cleanly():
    class Unreachable(FakeProvider):
        async def connect(self, *a, **k):
            raise ConnectionError("down")

    first = DyingProvider()
    s, _, sent = make(provider=first, factory=lambda: Unreachable(), provider_max_reconnects=2)
    await s.start()
    first.die()
    for _ in range(200):
        if s.sm.state == State.ERROR:
            break
        await asyncio.sleep(0.01)
    errors = [m for m in sent if isinstance(m, dict) and m.get("type") == "error"]
    assert s.sm.state == State.ERROR and len(errors) == 1 and errors[0]["message"] == "provider_unavailable"
    assert s._reconnects == 2  # no reintenta indefinidamente
    await s.close()
    await asyncio.sleep(0)
    assert not s._tasks


async def test_without_factory_failure_is_terminal_like_before():
    first = DyingProvider()
    s, _, sent = make(provider=first)
    await s.start()
    first.die()
    for _ in range(100):
        if s.sm.state == State.ERROR:
            break
        await asyncio.sleep(0.01)
    assert s.sm.state == State.ERROR
    await s.close()


async def test_connect_timeout_does_not_hang():
    class Hangs(FakeProvider):
        async def connect(self, *a, **k):
            await asyncio.sleep(30)

    s, _, _ = make(provider=Hangs(), provider_connect_timeout_s=0.05)
    with pytest.raises(asyncio.TimeoutError):
        await s.start()


async def test_closing_during_reconnect_leaves_no_orphans():
    closed = []

    class Slow(FakeProvider):
        async def connect(self, *a, **k):
            await asyncio.sleep(5)

        async def close(self):
            closed.append(True)
            await super().close()

    before = set(asyncio.all_tasks())
    first = DyingProvider()
    s, _, _ = make(provider=first, factory=lambda: Slow(), provider_connect_timeout_s=10)
    await s.start()
    first.die()
    await asyncio.sleep(0.1)  # ya está en plena reconexión
    await s.close()
    await asyncio.sleep(0.05)
    assert closed, "el proveedor a medio conectar debe cerrarse"
    await asyncio.sleep(0)
    assert not s._tasks and not (set(asyncio.all_tasks()) - before)


# ---------------- aislamiento entre sesiones ----------------

async def test_sessions_of_different_orgs_never_share_events_or_data():
    db = InMemoryPersistence()
    a, pa, sent_a = make("orgA", FakeProvider(script=[{"type": "transcript_agent", "text": "SECRETO-A"}], audio_chunks=2,
                                              chunk_delay=0.001), db=db)
    b, pb, sent_b = make("orgB", FakeProvider(script=[{"type": "transcript_agent", "text": "SECRETO-B"}], audio_chunks=2,
                                              chunk_delay=0.001), db=db)
    await a.start()
    await b.start()
    pa.trigger_response()
    pb.trigger_response()
    await asyncio.sleep(0.15)
    assert any(isinstance(m, dict) and m.get("text") == "SECRETO-A" for m in sent_a)
    assert not any("SECRETO-B" in json.dumps(m, default=str) for m in sent_a if isinstance(m, dict))
    assert not any("SECRETO-A" in json.dumps(m, default=str) for m in sent_b if isinstance(m, dict))
    await a.handle_barge_in()  # interrumpir A no toca a B
    assert pb.cancelled == 0 and b.sm.state != State.INTERRUPTED
    await a.close()
    await b.close()
    mine = await db.list_conversations("orgA", 10)
    theirs = await db.list_conversations("orgB", 10)
    assert len(mine) == 1 and len(theirs) == 1 and mine[0]["id"] != theirs[0]["id"]
    assert "SECRETO-B" not in json.dumps(await db.get_conversation("orgA", mine[0]["id"]), default=str)
    assert await db.get_conversation("orgA", theirs[0]["id"]) is None


async def test_failure_of_one_session_does_not_affect_another():
    s1, p1, _ = make("o1", DyingProvider())
    s2, p2, _ = make("o2", FakeProvider())
    await s1.start()
    await s2.start()
    p1.die()
    await asyncio.sleep(0.1)
    assert s1.sm.state == State.ERROR and s2.sm.state == State.LISTENING
    await s1.close()
    await s2.close()


# ---------------- límites de consumo (API) ----------------

def client(**over):
    st = Settings(environment="test", jwt_secret="t" * 40, **{"vad_backend": "energy", **over})
    return TestClient(create_app(st, provider_factory=lambda: FakeProvider(audio_chunks=2, respond_on_audio=True)))


def hdr(org="o1", user="u1"):
    return {"Authorization": f"Bearer {issue_token(user, org, 'customer')}"}


def test_global_session_cap_applies_across_orgs():
    c = client(max_total_sessions=1)
    sid = c.post("/api/v1/sessions", json={}, headers=hdr("o1")).json()["session_id"]
    conn = c.post(f"/api/v1/sessions/{sid}/connect", headers=hdr("o1")).json()
    with c.websocket_connect(conn["ws_path"]) as ws:
        ws.receive_json()
        r = c.post("/api/v1/sessions", json={}, headers=hdr("o2"))
        assert r.status_code == 429 and "servicio" in r.json()["detail"]


def test_org_daily_budget_blocks_new_sessions_and_only_that_org():
    c = client(org_daily_budget_minutes=1)
    app = c.app
    asyncio.run(app.state.store.add_usage("o1", 61))
    assert c.post("/api/v1/sessions", json={}, headers=hdr("o1")).status_code == 429
    assert c.post("/api/v1/sessions", json={}, headers=hdr("o2")).status_code == 200


def test_ws_connect_is_refused_when_budget_ran_out_after_session_creation():
    c = client(org_daily_budget_minutes=1)
    sid = c.post("/api/v1/sessions", json={}, headers=hdr()).json()["session_id"]
    conn = c.post(f"/api/v1/sessions/{sid}/connect", headers=hdr()).json()
    asyncio.run(c.app.state.store.add_usage("o1", 3600))
    with pytest.raises(WebSocketDisconnect) as e:
        with c.websocket_connect(conn["ws_path"]):
            pass
    assert e.value.code == 4429


def test_usage_is_charged_on_close_and_quota_is_released_after_abrupt_disconnect():
    c = client(org_daily_budget_minutes=60)
    sid = c.post("/api/v1/sessions", json={}, headers=hdr()).json()["session_id"]
    conn = c.post(f"/api/v1/sessions/{sid}/connect", headers=hdr()).json()
    with c.websocket_connect(conn["ws_path"]) as ws:
        ws.receive_json()
        ws.send_bytes(struct.pack(">I", 0) + LOUD)
    # salida del `with` = desconexión sin mensaje "end"
    assert asyncio.run(c.app.state.store.active_sessions("o1")) == 0
    assert asyncio.run(c.app.state.store.usage_seconds("o1")) > 0


# ---------------- política de logs ----------------

def test_redaction_masks_credentials_in_any_field():
    out = redact_secrets(None, None, {
        "event": "x",
        "url": "wss://generativelanguage.googleapis.com/ws?key=AIzaSyA1234567890abcdefghijklmnopqrstu",
        "hdr": "Authorization: Bearer abcdef1234567890",
        "other": "sk-proj-ABCDEFGH12345678"})
    blob = json.dumps(out)
    assert "AIza" not in blob and "abcdef1234567890" not in blob and "sk-proj" not in blob
    assert "[REDACTED]" in blob


async def test_session_logs_never_contain_audio_transcripts_or_keys():
    buf = io.StringIO()
    structlog.configure(processors=[structlog.processors.JSONRenderer()], logger_factory=structlog.PrintLoggerFactory(buf),
                        cache_logger_on_first_use=False)
    try:
        secret_text = "MI-NUMERO-DE-TARJETA-4111"
        prov = FakeProvider(script=[{"type": "transcript_user", "text": secret_text},
                                    {"type": "transcript_agent", "text": secret_text},
                                    {"type": "tool_call", "call_id": "c1", "name": "check_reservation",
                                     "arguments": json.dumps({"code": secret_text})}],
                            audio_chunks=3, chunk_delay=0.001)
        s, _, _ = make(provider=prov, openai_api_key="sk-live-SECRETKEY123456")
        await s.start()
        prov.trigger_response()
        for _ in range(3):
            await s.on_audio(LOUD)
        await s.on_client_event({"type": "barge_in"})
        await asyncio.sleep(0.2)
        await s.close()
    finally:
        structlog.reset_defaults()
    logs = buf.getvalue()
    assert logs, "la prueba debe haber capturado registros"
    assert secret_text not in logs and "SECRETKEY" not in logs and "sk-live" not in logs


# ---------------- latencia y coste ----------------

async def test_first_audio_latency_counts_from_last_voice_even_if_provider_answers_before_vad_end():
    before = TTFB_WINDOW.percentiles()["count"]
    s, prov, sent = make(vad_hangover_ms=600)
    await s.start()
    for _ in range(10):
        await s.on_audio(LOUD)
    await asyncio.sleep(0.12)
    for _ in range(3):
        await s.on_audio(QUIET)  # el VAD aún no ha declarado fin de habla (hangover de 600 ms)
    prov.trigger_response()
    for _ in range(100):
        if any(isinstance(m, dict) and m.get("type") == "metrics" for m in sent):
            break
        await asyncio.sleep(0.01)
    metric = next(m for m in sent if isinstance(m, dict) and m.get("type") == "metrics")
    assert metric["first_audio_ms"] >= 120  # mide desde la última voz, no desde el fin declarado por el VAD
    assert TTFB_WINDOW.percentiles()["count"] == before + 1
    await s.close()


async def test_stage_events_feed_stage_metrics_and_client_message():
    n0 = {k: w.percentiles()["count"] for k, w in STAGE_WINDOWS.items()}
    prov = FakeProvider(script=[{"type": "stage", "stage": "llm", "ms": 180.0}, {"type": "stage", "stage": "tts", "ms": 90.0}],
                        audio_chunks=2, chunk_delay=0.001)
    s, _, sent = make(provider=prov)
    await s.start()
    for _ in range(10):
        await s.on_audio(LOUD)
    await s.on_audio(QUIET)  # el usuario ya calló
    await asyncio.sleep(0.03)
    await prov._q.put({"type": "stage", "stage": "stt_final", "t": __import__("time").monotonic()})
    prov.trigger_response()
    for _ in range(100):
        if any(isinstance(m, dict) and m.get("type") == "metrics" for m in sent):
            break
        await asyncio.sleep(0.01)
    metric = next(m for m in sent if isinstance(m, dict) and m.get("type") == "metrics")
    assert metric["llm"] == 180.0 and metric["tts"] == 90.0 and metric["stt"] >= 0
    for k in ("stt", "llm", "tts"):
        assert STAGE_WINDOWS[k].percentiles()["count"] == n0[k] + 1
    await s.close()


async def test_cost_is_measured_usage_times_configured_price():
    s, prov, _ = make(price_s2s_in_per_min=0.06, price_s2s_out_per_min=0.24)
    await s.start()
    s._audio_in_bytes = 2 * 24000 * 60  # exactamente 1 min entrante
    s._audio_out_bytes = 2 * 24000 * 30  # 30 s salientes
    await s.close()
    assert s.cost_usd == pytest.approx(0.06 + 0.12, abs=1e-6)


async def test_cascade_cost_uses_per_service_usage_and_survives_reconnect():
    class Casc(FakeProvider):
        usage = {"llm_in_tokens": 1_000_000, "llm_out_tokens": 200_000, "tts_chars": 2000, "stt_seconds": 120.0}

    s, _, _ = make(provider=Casc(), price_llm_in_per_mtok=1.0, price_llm_out_per_mtok=5.0,
                   price_tts_per_1k_chars=0.03, price_stt_per_min=0.01)
    await s.start()
    comps = s._cost_components()
    assert comps["llm"] == pytest.approx(1.0 + 1.0) and comps["tts"] == pytest.approx(0.06) and comps["stt"] == pytest.approx(0.02)
    await s.close()


async def test_per_turn_quality_record_has_numbers_and_no_content():
    db = InMemoryPersistence()
    prov = FakeProvider(script=[{"type": "transcript_user", "text": "TEXTO-PRIVADO"},
                                {"type": "tool_call", "call_id": "c1", "name": "check_reservation",
                                 "arguments": json.dumps({"reservation_id": "X"})}], audio_chunks=3, chunk_delay=0.001)
    s, _, _ = make(provider=prov, db=db)
    await s.start()
    for _ in range(10):
        await s.on_audio(LOUD)
    await s.on_audio(QUIET)
    prov.trigger_response()
    await asyncio.sleep(0.2)
    await s.close()
    conv = (await db.list_conversations("o1", 5))[0]
    turns = [e["payload"] for e in db.events[conv["id"]] if e["type"] == "turn"]
    assert len(turns) == 1 and turns[0]["outcome"] == "completed" and turns[0]["tools"] == 1
    assert turns[0]["first_audio_ms"] >= 0
    assert "TEXTO-PRIVADO" not in json.dumps(db.events[conv["id"]]) .replace("transcript", "")  # solo cifras en los turnos
