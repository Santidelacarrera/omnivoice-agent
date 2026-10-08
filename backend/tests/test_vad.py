import asyncio
import math
import struct

import pytest

from app.core.config import Settings
from app.orchestration.session import VoiceSession
from app.realtime import vad as vadmod
from app.realtime.provider import FakeProvider
from app.realtime.vad import ModelVoiceActivityDetector, VoiceActivityDetector, create_vad, resample_pcm16, rms_pcm16
from app.security.auth import Principal
from app.tools.handlers import InMemoryRepository, build_registry

RATE = 24000
LOUD = struct.pack("<480h", *([12000, -12000] * 240))  # 20 ms a 24 kHz
QUIET = b"\x00\x00" * 480


def tone(freq: float, ms: int, amp: int = 9000, rate: int = RATE) -> bytes:
    n = rate * ms // 1000
    return struct.pack(f"<{n}h", *[int(amp * math.sin(2 * math.pi * freq * i / rate)) for i in range(n)])


def loud_classifier(frame: bytes, rate: int) -> bool:
    """Clasificador de prueba: voz = energía alta. Permite probar la lógica sin el módulo nativo."""
    return rms_pcm16(frame) > 0.05


def test_resample_24k_to_16k_keeps_duration_and_shape():
    out = resample_pcm16(tone(200, 20), 24000, 16000)
    assert len(out) == 320 * 2  # 20 ms a 16 kHz
    assert rms_pcm16(out) == pytest.approx(rms_pcm16(tone(200, 20)), rel=0.05)


def test_resample_is_noop_for_same_rate():
    assert resample_pcm16(LOUD, 24000, 24000) == LOUD


def test_model_vad_hysteresis_start_and_end():
    v = ModelVoiceActivityDetector(loud_classifier, min_speech_ms=60, sample_rate=RATE, hangover_ms=100)
    events = [v.feed(LOUD) for _ in range(6)] + [v.feed(QUIET) for _ in range(10)]
    assert events.count("speech_start") == 1 and events.count("speech_end") == 1
    assert events.index("speech_start") < events.index("speech_end")


def test_model_vad_short_pause_does_not_end_speech():
    v = ModelVoiceActivityDetector(loud_classifier, min_speech_ms=40, sample_rate=RATE, hangover_ms=200)
    events = [v.feed(LOUD) for _ in range(4)] + [v.feed(QUIET) for _ in range(3)] + [v.feed(LOUD) for _ in range(3)]
    assert "speech_end" not in events


def test_model_vad_accepts_chunks_of_any_size():
    v = ModelVoiceActivityDetector(loud_classifier, min_speech_ms=40, sample_rate=RATE, hangover_ms=100)
    data = LOUD * 6
    events = [v.feed(data[i:i + 300]) for i in range(0, len(data), 300)]  # 150 muestras: no alinea con 20 ms
    assert "speech_start" in events


def test_create_vad_falls_back_to_energy_when_model_missing(monkeypatch):
    def boom(_aggr):
        raise ImportError("webrtcvad no instalado")

    monkeypatch.setattr(vadmod, "_webrtc_classifier", boom)
    v = create_vad(Settings(vad_backend="webrtc"))
    assert type(v) is VoiceActivityDetector


def test_create_vad_energy_backend():
    assert type(create_vad(Settings(vad_backend="energy"))) is VoiceActivityDetector


def test_real_webrtc_model_ignores_digital_silence():
    pytest.importorskip("webrtcvad")
    v = create_vad(Settings(vad_backend="webrtc"))
    assert isinstance(v, ModelVoiceActivityDetector)
    assert not any(v.feed(QUIET) for _ in range(50))


# ---- confirmación de barge-in: el navegador sugiere, el servidor decide ----
def make_model_session(confirm_ms: int = 60):
    sent: list = []

    async def send(m):
        sent.append(m)

    s = VoiceSession(Principal("u1", "o1", "customer"), FakeProvider(), build_registry(InMemoryRepository()), send,
                     Settings(sample_rate=RATE, vad_backend="energy", barge_in_confirm_ms=confirm_ms))
    s.vad = ModelVoiceActivityDetector(loud_classifier, min_speech_ms=40, sample_rate=RATE, hangover_ms=200)
    return s, sent


def speaking(s):
    s._agent_speaking = True
    from app.orchestration.state_machine import State

    s._set(State.LISTENING)
    s._set(State.PROCESSING)
    s._set(State.RESPONDING)


@pytest.mark.asyncio
async def test_unconfirmed_hint_resumes_playback():
    s, sent = make_model_session(confirm_ms=40)
    await s.start()
    speaking(s)
    await s.on_client_event({"type": "barge_in"})
    assert {"type": "audio.clear"} not in sent  # todavía no: falta confirmación del servidor
    await asyncio.sleep(0.12)
    assert {"type": "audio.resume"} in sent and {"type": "audio.clear"} not in sent
    assert s._agent_speaking  # el agente sigue hablando: era ruido
    await s.close()


@pytest.mark.asyncio
async def test_confirmed_hint_interrupts_agent():
    s, sent = make_model_session(confirm_ms=500)
    await s.start()
    speaking(s)
    await s.on_client_event({"type": "barge_in"})
    for _ in range(4):
        await s.on_audio(LOUD)  # el modelo del servidor oye voz
    assert {"type": "audio.clear"} in sent
    await asyncio.sleep(0.05)
    assert {"type": "audio.resume"} not in sent  # la pista pendiente se canceló al confirmarse
    await s.close()
