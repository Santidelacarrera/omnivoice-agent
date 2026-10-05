"""VAD por energía (RMS) sobre PCM16 mono. Ligero, sin dependencias externas.

Se usa como segunda línea de defensa en servidor; el cliente ejecuta su propio
VAD en AudioWorklet para cortar la reproducción local sin esperar a la red.
"""
import array
import math


def rms_pcm16(chunk: bytes) -> float:
    if len(chunk) < 2:
        return 0.0
    samples = array.array("h")
    samples.frombytes(chunk[: len(chunk) // 2 * 2])
    if not samples:
        return 0.0
    acc = sum(s * s for s in samples)
    return math.sqrt(acc / len(samples)) / 32768.0


class VoiceActivityDetector:
    def __init__(self, threshold: float, min_speech_ms: int, sample_rate: int, hangover_ms: int = 600):
        self.threshold = threshold
        self.min_speech_ms = min_speech_ms
        self.hangover_ms = hangover_ms
        self.sample_rate = sample_rate
        self._speech_ms = 0.0
        self._silence_ms = 0.0
        self.speaking = False

    def feed(self, chunk: bytes) -> str | None:
        """Devuelve 'speech_start', 'speech_end' o None."""
        ms = (len(chunk) / 2) / self.sample_rate * 1000
        if rms_pcm16(chunk) >= self.threshold:
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
