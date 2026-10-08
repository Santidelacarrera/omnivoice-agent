import asyncio
import io
import struct
import wave

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect  # noqa: F401

from app.core.config import Settings
from app.main import create_app
from app.orchestration.session import SessionOptions, VoiceSession
from app.realtime.provider import FakeProvider
from app.security.auth import Principal, issue_token
from app.services.recordings import (LocalRecordingStorage, S3RecordingStorage, StereoRecorder, recording_key)
from app.tools.handlers import InMemoryRepository, build_registry

RATE = 24000
LOUD = struct.pack("<480h", *([12000, -12000] * 240))


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def samples(wav_bytes: bytes):
    with wave.open(io.BytesIO(wav_bytes)) as w:
        raw = w.readframes(w.getnframes())
        return w.getnchannels(), w.getframerate(), struct.unpack(f"<{len(raw) // 2}h", raw)


def test_recorder_aligns_tracks_and_pads_silence():
    clk = FakeClock()
    r = StereoRecorder(RATE, 60, clock=clk)
    clk.t = 0.02
    r.add_user(LOUD)  # llega a los 20 ms: ocupa [0, 480)
    clk.t = 0.06
    r.add_agent(b"\x07\x00" * 480)  # a los 60 ms: 20 ms de audio terminando en 60 ms → hueco de 40 ms antes
    ch, rate, data = samples(r.to_wav())
    assert (ch, rate) == (2, RATE)
    left, right = data[0::2], data[1::2]
    assert left[0] == 12000 and left[1] == -12000
    assert all(v == 0 for v in right[:960])  # silencio del agente antes de hablar (40 ms = 960 muestras)
    assert right[960] == 7 and len(right) == len(left) == 1440


def test_recorder_truncates_pending_agent_audio_on_barge_in():
    clk = FakeClock()
    r = StereoRecorder(RATE, 60, clock=clk)
    clk.t = 0.02
    r.add_agent(b"\x05\x00" * 480 * 10)  # ráfaga de 200 ms que aún no ha sonado
    clk.t = 0.04
    r.drop_pending_agent_audio()  # solo lo ya reproducido (40 ms) se conserva
    _, _, data = samples(r.to_wav())
    assert len(data[1::2]) == int(0.04 * RATE)


def test_recorder_respects_max_duration():
    clk = FakeClock()
    r = StereoRecorder(RATE, 1, clock=clk)
    for i in range(1, 80):
        clk.t = i * 0.02
        r.add_user(LOUD)
    assert r.truncated and r.seconds <= 1.0


def test_recorder_discard_leaves_nothing():
    r = StereoRecorder(RATE, 10)
    r.add_user(LOUD)
    r.discard()
    r.add_user(LOUD)  # tras descartar no acumula
    assert r.closed


def test_recording_key_layout():
    assert recording_key("org1", "conv1", now=0).startswith("org1/1970/01/conv1.wav")


@pytest.mark.asyncio
async def test_local_storage_roundtrip_and_path_traversal(tmp_path):
    st = LocalRecordingStorage(str(tmp_path))
    await st.put("o/2026/10/c.wav", b"abc")
    assert await st.get("o/2026/10/c.wav") == b"abc"
    await st.delete("o/2026/10/c.wav")
    assert await st.get("o/2026/10/c.wav") is None
    with pytest.raises(ValueError):
        await st.put("../../etc/passwd", b"x")


class FakeS3:
    def __init__(self):
        self.objects, self.puts = {}, []

    def put_object(self, **kw):
        self.puts.append(kw)
        self.objects[kw["Key"]] = kw["Body"]

    def get_object(self, Bucket, Key):
        class B:
            def read(_s):
                return self.objects[Key]

        return {"Body": B()}

    def delete_object(self, Bucket, Key):
        self.objects.pop(Key, None)

    def generate_presigned_url(self, op, Params, ExpiresIn):
        return f"https://s3.test/{Params['Key']}?exp={ExpiresIn}"


@pytest.mark.asyncio
async def test_s3_storage_uses_server_side_encryption_and_presigned_urls():
    fake = FakeS3()
    st = S3RecordingStorage("bkt", client=fake, sse="aws:kms", kms_key_id="key-1")
    await st.put("o/c.wav", b"data")
    assert fake.puts[0]["ServerSideEncryption"] == "aws:kms" and fake.puts[0]["SSEKMSKeyId"] == "key-1"
    assert await st.get("o/c.wav") == b"data"
    assert (await st.presigned_url("o/c.wav", 300)) == "https://s3.test/o/c.wav?exp=300"
    await st.delete("o/c.wav")
    assert "o/c.wav" not in fake.objects


# ---------- consentimiento en la sesión ----------
def make_session(storage, consent: bool, allowed: bool = True):
    sent = []

    async def send(m):
        sent.append(m)

    from app.services.persistence import InMemoryPersistence

    db = InMemoryPersistence()
    s = VoiceSession(Principal("u1", "o1", "customer"), FakeProvider(), build_registry(InMemoryRepository()), send,
                     Settings(sample_rate=RATE, vad_backend="energy"), persistence=db,
                     options=SessionOptions(recording_consent=consent, voice="alloy", language="es"),
                     recording_storage=storage, recording_allowed=allowed)
    return s, sent, db


@pytest.mark.asyncio
async def test_no_consent_means_no_recording(tmp_path):
    s, sent, db = make_session(LocalRecordingStorage(str(tmp_path)), consent=False)
    await s.start()
    assert s.recorder is None
    await s.on_audio(LOUD, 0)
    await s.close()
    assert db.recordings == {} and not list(tmp_path.rglob("*.wav"))
    assert sent[0]["recording"] is False


@pytest.mark.asyncio
async def test_policy_off_means_no_recording_even_with_consent(tmp_path):
    s, _, db = make_session(LocalRecordingStorage(str(tmp_path)), consent=True, allowed=False)
    await s.start()
    assert s.recorder is None
    await s.close()
    assert db.recordings == {}


@pytest.mark.asyncio
async def test_consent_records_and_stores_wav_with_metadata(tmp_path):
    s, sent, db = make_session(LocalRecordingStorage(str(tmp_path)), consent=True)
    await s.start()
    assert sent[0]["recording"] is True and sent[0]["voice"] == "alloy" and sent[0]["language"] == "es"
    for i in range(5):
        await s.on_audio(LOUD, i)
    await s.close()
    files = list(tmp_path.rglob("*.wav"))
    assert len(files) == 1
    conv = next(iter(db.convs.values()))
    assert conv["recording_consent"] is True and conv["voice"] == "alloy" and conv["consent_at"]
    rec = db.recordings[conv["id"]][0]
    assert rec["storage_key"].endswith(f"{conv['id']}.wav") and rec["bytes"] == files[0].stat().st_size
    actions = [a["action"] for a in db.audits]
    assert "recording.consent" in actions and "recording.stored" in actions


@pytest.mark.asyncio
async def test_revoking_consent_discards_audio(tmp_path):
    s, sent, db = make_session(LocalRecordingStorage(str(tmp_path)), consent=True)
    await s.start()
    await s.on_audio(LOUD, 0)
    await s.on_client_event({"type": "recording_revoke"})
    await s.on_audio(LOUD, 1)
    await s.close()
    assert not list(tmp_path.rglob("*.wav")) and db.recordings == {}
    assert {"type": "recording", "active": False} in sent
    assert next(iter(db.convs.values()))["recording_consent"] is False
    assert "recording.revoked" in [a["action"] for a in db.audits]


class BrokenStorage(LocalRecordingStorage):
    async def put(self, key, data, content_type="audio/wav"):
        raise OSError("disco lleno")


@pytest.mark.asyncio
async def test_storage_failure_does_not_break_session_close(tmp_path):
    s, _, db = make_session(BrokenStorage(str(tmp_path)), consent=True)
    await s.start()
    await s.on_audio(LOUD, 0)
    await s.close()  # no lanza
    assert db.recordings == {}
    assert "recording.failed" in [a["action"] for a in db.audits]
    assert next(iter(db.convs.values()))["final_state"] == "COMPLETED"


# ---------- API: catálogo, validación y flujo con grabación ----------
def api_client(tmp_path, **overrides):
    settings = Settings(environment="test", jwt_secret="t" * 40, vad_backend="energy", recording_storage="local",
                        recording_local_dir=str(tmp_path), **overrides)
    app = create_app(settings, provider_factory=lambda: FakeProvider(audio_chunks=3, respond_on_audio=True))
    return app, TestClient(app)


def auth(role="customer", org="o1", user="u1"):
    return {"Authorization": f"Bearer {issue_token(user, org, role)}"}


def wait_closed(app, timeout=5.0):
    """El cierre de la sesión corre en el servidor tras salir del `with` del cliente: se espera a su auditoría."""
    import time

    end = time.time() + timeout
    while time.time() < end:
        if "session.completed" in [a["action"] for a in app.state.db.audits]:
            return
        time.sleep(0.02)


def test_catalog_lists_voices_languages_and_recording_availability(tmp_path):
    _, c = api_client(tmp_path)
    cat = c.get("/api/v1/catalog", headers=auth()).json()
    assert "alloy" in cat["voices"] and {"code": "es", "name": "español"} in cat["languages"]
    assert cat["recording"]["available"] is True


def test_recording_unavailable_without_storage(tmp_path):
    settings = Settings(environment="test", jwt_secret="t" * 40, vad_backend="energy")  # recording_storage=none
    c = TestClient(create_app(settings, provider_factory=lambda: FakeProvider()))
    assert c.get("/api/v1/catalog", headers=auth()).json()["recording"]["available"] is False
    r = c.post("/api/v1/sessions", json={"recording_consent": True}, headers=auth()).json()
    assert r["recording"] is False  # el consentimiento no activa nada si la grabación no está disponible


def test_invalid_voice_or_language_is_rejected(tmp_path):
    _, c = api_client(tmp_path)
    assert c.post("/api/v1/sessions", json={"voice": "hacker<script>"}, headers=auth()).status_code == 422
    assert c.post("/api/v1/sessions", json={"language": "xx"}, headers=auth()).status_code == 422
    assert c.post("/api/v1/sessions", json={"voice": "alloy", "language": "en-US"}, headers=auth()).status_code == 200


def test_org_policy_blocks_recording(tmp_path):
    app, c = api_client(tmp_path)
    app.state.db.recording_enabled_orgs = {"otra-org"}
    r = c.post("/api/v1/sessions", json={"recording_consent": True}, headers=auth(org="o1")).json()
    assert r["recording"] is False


def test_full_flow_with_consent_stores_recording_and_only_admin_can_download(tmp_path):
    app, c = api_client(tmp_path)
    h = auth()
    sess = c.post("/api/v1/sessions", json={"recording_consent": True, "voice": "alloy", "language": "es"}, headers=h).json()
    assert sess["recording"] is True
    conn = c.post(f"/api/v1/sessions/{sess['session_id']}/connect", headers=h).json()
    with c.websocket_connect(conn["ws_path"]) as ws:
        ready = ws.receive_json()
        assert ready["type"] == "session.ready" and ready["recording"] is True
        for i in range(6):
            ws.send_bytes(struct.pack(">I", i) + LOUD)
        ws.send_text('{"type":"end"}')
    wait_closed(app)
    cid = c.get("/api/v1/conversations", headers=auth("operator")).json()["items"][0]["id"]
    assert c.get(f"/api/v1/conversations/{cid}/recording", headers=auth("operator")).status_code == 403
    r = c.get(f"/api/v1/conversations/{cid}/recording", headers=auth("admin"))
    acts = [a["action"] for a in app.state.db.audits]
    assert r.status_code == 200, (r.text, acts, app.state.db.recordings)
    assert r.content[:4] == b"RIFF" and r.headers["content-type"] == "audio/wav"
    assert "recording.accessed" in [a["action"] for a in app.state.db.audits]


def test_other_org_cannot_download_recording(tmp_path):
    app, c = api_client(tmp_path)
    h = auth(org="o1")
    sess = c.post("/api/v1/sessions", json={"recording_consent": True}, headers=h).json()
    conn = c.post(f"/api/v1/sessions/{sess['session_id']}/connect", headers=h).json()
    with c.websocket_connect(conn["ws_path"]) as ws:
        ws.receive_json()
        ws.send_bytes(struct.pack(">I", 0) + LOUD)
        ws.send_text('{"type":"end"}')
    wait_closed(app)
    cid = c.get("/api/v1/conversations", headers=auth("operator", org="o1")).json()["items"][0]["id"]
    assert c.get(f"/api/v1/conversations/{cid}/recording", headers=auth("admin", org="o1")).status_code == 200  # existe
    assert c.get(f"/api/v1/conversations/{cid}/recording", headers=auth("admin", org="o2")).status_code == 404
