import math
import time
from collections import deque
from dataclasses import dataclass, field

import structlog
from prometheus_client import Counter, Gauge, Histogram

log = structlog.get_logger()

TTFB = Histogram(
    "omnivoice_first_audio_seconds",
    "Tiempo desde fin de habla del usuario hasta el primer audio de respuesta",
    buckets=(0.1, 0.2, 0.3, 0.5, 0.75, 1, 2, 5),
)
BARGE_IN = Histogram(
    "omnivoice_barge_in_seconds",
    "Tiempo desde detección de voz hasta silencio efectivo",
    buckets=(0.025, 0.05, 0.1, 0.2, 0.4, 1),
)
TOOL_DURATION = Histogram("omnivoice_tool_seconds", "Duración de herramientas", ["tool", "status"])
ACTIVE_SESSIONS = Gauge("omnivoice_active_sessions", "Sesiones activas")
ERRORS = Counter("omnivoice_errors_total", "Errores", ["kind"])
PACKETS_LOST = Counter("omnivoice_packets_lost_total", "Paquetes de audio perdidos (huecos de secuencia)")


class LatencyWindow:
    """Ventana deslizante de muestras para calcular p50/p95/p99 por proceso (sin dependencias)."""

    def __init__(self, size: int = 2000) -> None:
        self._d: deque[float] = deque(maxlen=size)

    def add(self, v: float) -> None:
        self._d.append(v)

    def percentiles(self, scale: float = 1.0) -> dict[str, float | int | None]:
        data = sorted(self._d)
        n = len(data)
        if n == 0:
            return {"count": 0, "p50": None, "p95": None, "p99": None}

        def pct(q: float) -> float:
            idx = min(n - 1, max(0, math.ceil(q * n) - 1))  # método nearest-rank
            return round(data[idx] * scale, 1)

        return {"count": n, "p50": pct(0.50), "p95": pct(0.95), "p99": pct(0.99)}


TTFB_WINDOW = LatencyWindow()
BARGE_WINDOW = LatencyWindow()


@dataclass
class SessionMetrics:
    session_id: str
    org_id: str
    started_at: float = field(default_factory=time.monotonic)
    user_speech_end: float | None = None
    first_audio_at: float | None = None
    barge_detected_at: float | None = None
    interruptions: int = 0
    last_seq: int | None = None
    lost_packets: int = 0

    def mark_user_speech_end(self) -> None:
        self.user_speech_end = time.monotonic()
        self.first_audio_at = None

    def mark_audio_out(self) -> float | None:
        if self.user_speech_end is not None and self.first_audio_at is None:
            self.first_audio_at = time.monotonic()
            ttfb = self.first_audio_at - self.user_speech_end
            TTFB.observe(ttfb)
            TTFB_WINDOW.add(ttfb)
            return ttfb
        return None

    def mark_barge_detected(self) -> None:
        self.barge_detected_at = time.monotonic()
        self.interruptions += 1

    def mark_silenced(self) -> float | None:
        if self.barge_detected_at is None:
            return None
        dt = time.monotonic() - self.barge_detected_at
        BARGE_IN.observe(dt)
        BARGE_WINDOW.add(dt)
        self.barge_detected_at = None
        return dt

    def track_seq(self, seq: int) -> None:
        if self.last_seq is not None and seq > self.last_seq + 1:
            gap = seq - self.last_seq - 1
            self.lost_packets += gap
            PACKETS_LOST.inc(gap)
        self.last_seq = seq
