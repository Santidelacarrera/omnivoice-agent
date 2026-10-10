"""Detección de actividad de voz (VAD) sobre PCM16 mono.

Dos implementaciones con la misma interfaz (`feed(chunk) -> 'speech_start' | 'speech_end' | None`):

* `VoiceActivityDetector`: por energía (RMS). Ligera, sin dependencias, pero confunde ruido fuerte con voz.
* `ModelVoiceActivityDetector`: clasifica tramas de 20 ms con WebRTC VAD (modelo estadístico de GMM
  entrenado para distinguir voz de ruido), con histéresis para no cortar por pausas breves.

`create_vad(settings)` elige según `VAD_BACKEND` y vuelve a energía, avisando en el log, si el módulo
nativo no está disponible. El cliente ejecuta además su propio VAD en el navegador solo para pausar la
reproducción al instante; el servidor decide con este módulo si es voz real (ver `VoiceSession`).
"""
import array
import math
from typing import Any, Callable

import structlog

log = structlog.get_logger()

MODEL_RATE = 16000  # WebRTC VAD acepta 8/16/32/48 kHz; la plataforma trabaja a 24 kHz
FRAME_MS = 20


def rms_pcm16(chunk: bytes) -> float:
    if len(chunk) < 2:
        return 0.0
    samples = array.array("h")
    samples.frombytes(chunk[: len(chunk) // 2 * 2])
    if not samples:
        return 0.0
    acc = sum(s * s for s in samples)
    return math.sqrt(acc / len(samples)) / 32768.0


def resample_pcm16(chunk: bytes, src_rate: int, dst_rate: int) -> bytes:
    """Remuestreo lineal de PCM16 mono (suficiente para clasificar voz; no es para reproducción)."""
    if src_rate == dst_rate or len(chunk) < 4:
        return chunk
    src = array.array("h")
    src.frombytes(chunk[: len(chunk) // 2 * 2])
    n_out = int(len(src) * dst_rate / src_rate)
    if n_out <= 0:
        return b""
    ratio = src_rate / dst_rate
    out = array.array("h", bytes(2 * n_out))
    last = len(src) - 1
    for i in range(n_out):
        pos = i * ratio
        j = int(pos)
        frac = pos - j
        a = src[j] if j <= last else src[last]
        b = src[j + 1] if j + 1 <= last else src[last]
        out[i] = int(a + (b - a) * frac)
    return out.tobytes()


class VoiceActivityDetector:
    """VAD por energía con histéresis. Las subclases solo cambian `_is_speech`."""

    def __init__(self, threshold: float, min_speech_ms: int, sample_rate: int, hangover_ms: int = 600):
        self.threshold = threshold
        self.min_speech_ms = min_speech_ms
        self.hangover_ms = hangover_ms
        self.sample_rate = sample_rate
        self._speech_ms = 0.0
        self._silence_ms = 0.0
        self.speaking = False
        self.last_voiced = False  # ¿el último chunk contenía voz? (distinto de `speaking`, que incluye el hangover)

    def _is_speech(self, chunk: bytes) -> bool:
        return rms_pcm16(chunk) >= self.threshold

    def feed(self, chunk: bytes) -> str | None:
        """Devuelve 'speech_start', 'speech_end' o None."""
        ms = (len(chunk) / 2) / self.sample_rate * 1000
        voiced = self._is_speech(chunk)
        self.last_voiced = voiced
        if voiced:
            self._speech_ms += ms
            self._silence_ms = 0.0
            if not self.speaking and self._speech_ms >= self.min_speech_ms:
                self.speaking = True
                return "speech_start"
        else:
            self._silence_ms += ms
            if not self.speaking:
                self._speech_ms = 0.0
            elif self._silence_ms >= self.hangover_ms:
                self.speaking = False
                self._speech_ms = 0.0
                return "speech_end"
        return None


class ModelVoiceActivityDetector(VoiceActivityDetector):
    """VAD con modelo: cada trama de 20 ms (a 16 kHz) la clasifica `classify(frame_bytes, 16000)`.

    Un chunk cuenta como voz si al menos la mitad de sus tramas completas lo son. Los bytes sobrantes
    se conservan para el siguiente chunk, de modo que acepta chunks de cualquier tamaño.
    """

    def __init__(self, classify: Callable[[bytes, int], bool], min_speech_ms: int, sample_rate: int,
                 hangover_ms: int = 600, min_speech_ratio: float = 0.5):
        super().__init__(threshold=0.0, min_speech_ms=min_speech_ms, sample_rate=sample_rate, hangover_ms=hangover_ms)
        self._classify = classify
        self._ratio = min_speech_ratio
        self._frame_bytes = MODEL_RATE * FRAME_MS // 1000 * 2
        self._pending = b""
        self._last = False  # última decisión, para chunks que no completan una trama

    def _is_speech(self, chunk: bytes) -> bool:
        data = self._pending + resample_pcm16(chunk, self.sample_rate, MODEL_RATE)
        n_frames = len(data) // self._frame_bytes
        self._pending = data[n_frames * self._frame_bytes:]
        if n_frames == 0:
            return self._last  # sin trama completa: se mantiene la última decisión
        voiced = sum(
            1 for k in range(n_frames)
            if self._classify(data[k * self._frame_bytes:(k + 1) * self._frame_bytes], MODEL_RATE)
        )
        self._last = voiced / n_frames >= self._ratio
        return self._last


def _webrtc_classifier(aggressiveness: int) -> Callable[[bytes, int], bool]:
    import webrtcvad  # módulo nativo (paquete `webrtcvad-wheels`)

    vad = webrtcvad.Vad(max(0, min(3, aggressiveness)))
    return lambda frame, rate: vad.is_speech(frame, rate)


def create_vad(settings: Any) -> VoiceActivityDetector:
    """Crea el VAD configurado. Si el modelo no está disponible, usa energía y lo deja en el log."""
    if getattr(settings, "vad_backend", "energy") == "webrtc":
        try:
            classify = _webrtc_classifier(settings.vad_aggressiveness)
            return ModelVoiceActivityDetector(classify, settings.vad_min_speech_ms, settings.sample_rate,
                                              hangover_ms=settings.vad_hangover_ms)
        except Exception as exc:  # noqa: BLE001 - ImportError o fallo del módulo nativo
            log.warning("vad_model_unavailable_using_energy", error=type(exc).__name__, detail=str(exc)[:200])
    return VoiceActivityDetector(settings.vad_energy_threshold, settings.vad_min_speech_ms, settings.sample_rate,
                                 hangover_ms=getattr(settings, "vad_hangover_ms", 600))
