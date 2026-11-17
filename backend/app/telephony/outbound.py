"""Política de llamadas salientes: horarios permitidos, reintentos, consentimiento y huella del número.

Todo es puro (recibe `now` y devuelve decisiones) para poder probarlo sin reloj ni base de datos.

Principios (documento maestro 8.2 y 16):
- Nunca se llama sin una base de consentimiento declarada (`consent_basis`) y sin comprobar la lista de exclusión.
- Solo se llama dentro de la ventana horaria, en la zona horaria de quien recibe la llamada. La API no puede ampliarla.
- Reintentos limitados y espaciados; una llamada atendida no se reintenta.
- El número se guarda solo mientras haga falta para marcar; al terminar el trabajo se borra y queda una huella con sal por organización.
"""
import hashlib
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

CONSENT_BASES = ("explicit_opt_in", "existing_customer", "transactional")
RETRYABLE_CALL_STATES = ("BUSY", "NO_ANSWER", "FAILED")  # estados de llamada tras los que tiene sentido reintentar


class OutboundPolicyError(ValueError):
    """Entrada que incumple la política (mensaje seguro de mostrar)."""


@dataclass(frozen=True)
class OutboundPolicy:
    start: time
    end: time
    days: frozenset[int]  # 0 = lunes … 6 = domingo
    timezone: str
    max_attempts: int
    retry_base_minutes: int = 15
    retry_cap_minutes: int = 120


def parse_hhmm(value: str) -> time:
    try:
        h, m = value.split(":")
        return time(int(h), int(m))
    except (ValueError, AttributeError):
        raise OutboundPolicyError(f"Hora inválida: {value!r} (usa HH:MM)") from None


def parse_days(value: str) -> frozenset[int]:
    try:
        days = frozenset(int(x) for x in value.split(",") if x.strip() != "")
    except ValueError:
        raise OutboundPolicyError(f"Días inválidos: {value!r}") from None
    if not days or any(d < 0 or d > 6 for d in days):
        raise OutboundPolicyError("Los días van de 0 (lunes) a 6 (domingo)")
    return days


def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise OutboundPolicyError(f"Zona horaria desconocida: {name!r}") from None


def in_window(now_utc: datetime, policy: OutboundPolicy, tz: str | None = None) -> bool:
    local = now_utc.astimezone(zone(tz or policy.timezone))
    return local.weekday() in policy.days and policy.start <= local.time() < policy.end


def next_window_start(now_utc: datetime, policy: OutboundPolicy, tz: str | None = None) -> datetime:
    """Primer instante (UTC) en que se puede llamar: ahora si ya se puede, o el próximo inicio de ventana."""
    z = zone(tz or policy.timezone)
    if in_window(now_utc, policy, tz):
        return now_utc
    local = now_utc.astimezone(z)
    for add in range(0, 9):
        day = (local + timedelta(days=add)).date()
        if day.weekday() not in policy.days:
            continue
        start = datetime.combine(day, policy.start, tzinfo=z)
        if start > local:
            return start.astimezone(now_utc.tzinfo)
    raise OutboundPolicyError("La política no deja ningún día para llamar")  # no ocurre con días válidos


def retry_delay(policy: OutboundPolicy, attempts_done: int) -> timedelta:
    """Espera lineal creciente (15, 30, 45… min) con tope."""
    return timedelta(minutes=min(policy.retry_cap_minutes, policy.retry_base_minutes * max(1, attempts_done)))


def number_hash(org_id: str, e164: str) -> str:
    """Huella del número con sal por organización: sirve para duplicados y lista de exclusión sin guardar el número."""
    return hashlib.sha256(f"{org_id}:{e164}".encode()).hexdigest()


def validate_consent(basis: str, ref: str | None) -> None:
    if basis not in CONSENT_BASES:
        raise OutboundPolicyError(f"consent_basis debe ser uno de {list(CONSENT_BASES)}")
    if basis == "explicit_opt_in" and not (ref or "").strip():
        raise OutboundPolicyError("explicit_opt_in exige consent_ref (cómo y cuándo dio su consentimiento)")
