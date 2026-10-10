import math
import time
from collections import deque
from dataclasses import dataclass, field

import structlog
from prometheus_client import Counter, Gauge, Histogram

log = structlog.get_logger()

TTFB = Histogram(
    "omnivoice_first_audio_seconds",
    "Tiempo desde el último instante de voz del usuario hasta el primer audio de respuesta",
    buckets=(0.1, 0.2, 0.3, 0.5, 0.75, 1, 1.5, 2, 3, 5),
)
STAGE_LATENCY = Histogram(
    "omnivoice_stage_seconds",
    "Latencia por etapa del turno (solo proveedor en cascada): stt = fin de voz -> transcripción final; "
    "llm = transcripción -> primera frase; tts = primera frase -> primer audio",
    ["stage"],
    buckets=(0.05, 0.1, 0.2, 0.3, 0.5, 0.75, 1, 1.5, 2, 3, 5),
)
PROVIDER_RECONNECTS = Counter("omnivoice_provider_reconnects_total", "Reconexiones al proveedor de voz", ["outcome"])
CONVERSATION_SECONDS = Counter("omnivoice_conversation_seconds_total", "Segundos de conversación facturables")
CONVERSATION_COST = Counter("omnivoice_conversation_cost_usd_total", "Coste estimado de proveedores (USD)", ["component"])
BUDGET_REJECTED = Counter("omnivoice_budget_rejected_total", "Sesiones rechazadas por tope de consumo", ["reason"])
BARGE_IN = Histogram(
    "omnivoice_barge_in_seconds",
    "Tiempo desde detección de voz hasta silencio efectivo",
    buckets=(0.025, 0.05, 0.1, 0.2, 0.4, 1),
)
TOOL_DURATION = Histogram("omnivoice_tool_seconds", "Duración de herramientas", ["tool", "status"])
ACTIVE_SESSIONS = Gauge("omnivoice_active_sessions", "Sesiones activas")
ERRORS = Counter("omnivoice_errors_total", "Errores", ["kind"])
RETENTION_DELETED = Counter("omnivoice_retention_deleted_total", "Registros purgados por retención", ["kind"])
RETENTION_RUNS = Counter("omnivoice_retention_runs_total", "Pasadas completas del job de retención")
RECORDINGS = Counter("omnivoice_recordings_total", "Grabaciones", ["outcome"])
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
STAGE_WINDOWS: dict[str, LatencyWindow] = {"stt": LatencyWindow(), "llm": LatencyWindow(), "tts": LatencyWindow()}


class CostWindow:
    """Acumula segundos y coste por componente en el proceso actual (para el coste por minuto en /api/v1/metrics)."""

    def __init__(self) -> None:
        self.seconds = 0.0
        self.sessions = 0
        self.by_component: dict[str, float] = {}

    def add(self, seconds: float, components: dict[str, float]) -> None:
        self.seconds += seconds
        self.sessions += 1
        for k, v in components.items():
            self.by_component[k] = self.by_component.get(k, 0.0) + v
        CONVERSATION_SECONDS.inc(seconds)
        for k, v in components.items():
            CONVERSATION_COST.labels(k).inc(v)

    def summary(self) -> dict:
        total = sum(self.by_component.values())
        minutes = self.seconds / 60
        return {"sessions": self.sessions, "minutes": round(minutes, 3), "cost_usd": round(total, 6),
                "cost_per_minute_usd": round(total / minutes, 5) if minutes > 0 else None,
                "by_component_usd": {k: round(v, 6) for k, v in self.by_component.items()}}


COST_WINDOW = CostWindow()


@dataclass
class SessionMetrics:
    session_id: str
    org_id: str
    started_at: float = field(default_factory=time.monotonic)
    last_voice_at: float | None = None  # último chunk con voz del usuario: el fin real del turno
    awaiting_response: bool = False
    first_audio_at: float | None = None
    barge_detected_at: float | None = None
    interruptions: int = 0
    last_seq: int | None = None
    lost_packets: int = 0

    def mark_voice(self) -> None:
        """Cada chunk con voz mueve el fin del turno; la latencia se mide desde el ÚLTIMO, no desde que el VAD
        declara fin de habla (eso añadiría `vad_hangover_ms` de silencio que el usuario no percibe como latencia
        del sistema, o lo ocultaría si el proveedor responde antes)."""
        self.last_voice_at = time.monotonic()
        self.awaiting_response = True
        self.first_audio_at = None

    def mark_audio_out(self) -> float | None:
        """Primer audio de la respuesta a un turno del usuario. Devuelve la latencia o None (saludo, audio posterior)."""
        if self.awaiting_response and self.last_voice_at is not None:
            self.awaiting_response = False
            self.first_audio_at = time.monotonic()
            ttfb = self.first_audio_at - self.last_voice_at
            TTFB.observe(ttfb)
            TTFB_WINDOW.add(ttfb)
            return ttfb
        return None

    def mark_stage(self, stage: str, seconds: float) -> None:
        seconds = max(0.0, seconds)
        STAGE_LATENCY.labels(stage).observe(seconds)
        STAGE_WINDOWS.setdefault(stage, LatencyWindow()).add(seconds)

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
