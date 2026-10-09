"""Códec de telefonía: μ-law 8 kHz (G.711) ↔ PCM16 24 kHz de la plataforma.

`audioop` desapareció en Python 3.13, así que se implementa con tablas (decodificar) y un cálculo corto (codificar).
"""
import array

from app.realtime.vad import resample_pcm16

TEL_RATE = 8000
BIAS = 0x84
CLIP = 32635


def _ulaw_to_linear(u: int) -> int:
    u = ~u & 0xFF
    t = (((u & 0x0F) << 3) + BIAS) << ((u & 0x70) >> 4)
    return (BIAS - t) if (u & 0x80) else (t - BIAS)


_DECODE = array.array("h", (_ulaw_to_linear(i) for i in range(256)))


def ulaw_decode(data: bytes) -> bytes:
    return array.array("h", (_DECODE[b] for b in data)).tobytes()


def _linear_to_ulaw(sample: int) -> int:
    sign = 0x80 if sample < 0 else 0
    if sample < 0:
        sample = -sample
    sample = min(sample, CLIP) + BIAS
    exponent = 7
    mask = 0x4000
    while exponent > 0 and not (sample & mask):
        exponent -= 1
        mask >>= 1
    mantissa = (sample >> (exponent + 3)) & 0x0F
    return ~(sign | (exponent << 4) | mantissa) & 0xFF


def ulaw_encode(pcm: bytes) -> bytes:
    s = array.array("h")
    s.frombytes(pcm[: len(pcm) // 2 * 2])
    return bytes(_linear_to_ulaw(v) for v in s)


def telephony_to_platform(ulaw: bytes, platform_rate: int) -> bytes:
    """μ-law 8 kHz recibido de la red → PCM16 a la frecuencia de la plataforma."""
    return resample_pcm16(ulaw_decode(ulaw), TEL_RATE, platform_rate)


def platform_to_telephony(pcm: bytes, platform_rate: int) -> bytes:
    """PCM16 de la plataforma → μ-law 8 kHz. Al reducir se promedia cada grupo de muestras (filtro simple
    contra aliasing) en vez de saltarlas."""
    if platform_rate % TEL_RATE == 0:
        k = platform_rate // TEL_RATE
        src = array.array("h")
        src.frombytes(pcm[: len(pcm) // 2 * 2])
        out = array.array("h", (sum(src[i:i + k]) // k for i in range(0, len(src) - k + 1, k)))
        return ulaw_encode(out.tobytes())
    return ulaw_encode(resample_pcm16(pcm, platform_rate, TEL_RATE))
