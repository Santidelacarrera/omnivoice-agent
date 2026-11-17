"""Sentry y OpenTelemetry, opcionales.

Ninguno es obligatorio ni está en `requirements.txt`: se activan solo si defines su variable y la librería está instalada
(`pip install -r requirements-optional.txt`). Si falta la librería se avisa en el log y la aplicación sigue funcionando.

Privacidad: la plataforma maneja voz y datos personales, así que Sentry se inicializa **sin** datos personales por defecto
(`send_default_pii=False`) y `scrub_event` elimina cabeceras de autorización, cuerpos de petición, cookies y cualquier valor cuyo nombre
sugiera un secreto, antes de enviar nada. Los números de teléfono y las transcripciones no deben llegar a ningún servicio externo.
"""
from typing import Any

import structlog

log = structlog.get_logger()

# Un campo se filtra si su nombre CONTIENE alguno de estos fragmentos, o si coincide exactamente con alguno de estos nombres.
_SENSITIVE_SUBSTRINGS = ("authorization", "cookie", "signature", "token", "secret", "password", "api_key", "apikey", "ticket")
_SENSITIVE_EXACT = {"key", "body", "data", "transcript", "text", "phone", "number", "to", "from"}


def _is_sensitive(name: Any) -> bool:
    n = str(name).lower()
    return n in _SENSITIVE_EXACT or any(part in n for part in _SENSITIVE_SUBSTRINGS)


def _scrub(value: Any, depth: int = 0) -> Any:
    if depth > 6:
        return "[recortado]"
    if isinstance(value, dict):
        return {k: ("[filtrado]" if _is_sensitive(k) else _scrub(v, depth + 1)) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v, depth + 1) for v in value]
    return value


def scrub_event(event: dict[str, Any], _hint: Any = None) -> dict[str, Any] | None:
    """`before_send` de Sentry: quita lo sensible de la petición y de los datos adjuntos."""
    req = event.get("request")
    if isinstance(req, dict):
        req.pop("data", None)  # cuerpo de la petición: puede traer transcripciones, números o credenciales
        req.pop("cookies", None)
        req.pop("query_string", None)  # puede llevar tickets o claves
        if isinstance(req.get("headers"), dict):
            req["headers"] = _scrub(req["headers"])
    for key in ("extra", "contexts", "user"):
        if key in event:
            event[key] = _scrub(event[key]) if key != "user" else {}
    for crumb in (event.get("breadcrumbs") or {}).get("values", []) if isinstance(event.get("breadcrumbs"), dict) else []:
        crumb.pop("data", None)
    return event


def setup_sentry(settings: Any) -> bool:
    """Activa Sentry si hay `SENTRY_DSN`. Devuelve si quedó activo."""
    if not getattr(settings, "sentry_dsn", ""):
        return False
    try:
        import sentry_sdk
    except ImportError:
        log.warning("sentry_not_installed", hint="pip install -r requirements-optional.txt")
        return False
    sentry_sdk.init(dsn=settings.sentry_dsn, environment=settings.environment, send_default_pii=False,
                    traces_sample_rate=settings.sentry_traces_sample_rate, before_send=scrub_event, max_request_body_size="never")
    return True


def setup_tracing(app: Any, settings: Any) -> bool:
    """Activa OpenTelemetry (trazas) si hay `OTEL_EXPORTER_OTLP_ENDPOINT`. Instrumenta FastAPI; no registra cuerpos ni cabeceras."""
    endpoint = getattr(settings, "otel_exporter_otlp_endpoint", "")
    if not endpoint:
        return False
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        log.warning("opentelemetry_not_installed", hint="pip install -r requirements-optional.txt")
        return False
    provider = TracerProvider(resource=Resource.create({"service.name": settings.otel_service_name, "deployment.environment": settings.environment}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint.rstrip("/") + "/v1/traces")))
    trace.set_tracer_provider(provider)
    FastAPIInstrumentor.instrument_app(app, excluded_urls="healthz,readyz,metrics")  # sin ruido de sondas
    return True
