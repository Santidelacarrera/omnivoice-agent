"""Panel de Grafana, Sentry y OpenTelemetry opcionales. Sentry y OTel se prueban con módulos falsos: no se ha verificado contra servicios reales."""
import json
import re
import sys
import types
from pathlib import Path

import pytest
import yaml

from app.core.config import Settings
from app.observability import telemetry
from app.observability.telemetry import scrub_event, setup_sentry, setup_tracing

ROOT = Path(__file__).resolve().parents[2]
GRAFANA = ROOT / "infrastructure" / "grafana"


# ---------- Grafana ----------
def test_grafana_dashboard_is_valid_and_only_uses_metrics_that_exist():
    dash = json.loads((GRAFANA / "dashboards" / "omnivoice.json").read_text(encoding="utf-8"))
    assert dash["uid"] == "omnivoice-overview" and len(dash["panels"]) >= 10
    ids = [p["id"] for p in dash["panels"]]
    assert len(ids) == len(set(ids))
    code = "\n".join(p.read_text(encoding="utf-8") for p in (ROOT / "backend" / "app").rglob("*.py"))
    declared = set(re.findall(r'(?:Counter|Histogram|Gauge)\(\s*"(omnivoice_[a-z_]+?)(?:_total)?"', code))
    for panel in dash["panels"]:
        assert panel["datasource"]["uid"] == "omnivoice-prometheus" and panel["targets"], panel["title"]
        for t in panel["targets"]:
            assert t["expr"].count("(") == t["expr"].count(")"), t["expr"]
            for metric in re.findall(r"\b(omnivoice_[a-z_]+)", t["expr"]):
                base = re.sub(r"_(bucket|total|count|sum)$", "", metric)
                assert base in declared or metric in declared, f"{panel['title']}: {metric} no existe en el código"


def test_grafana_provisioning_points_at_the_dashboard_and_the_datasource():
    ds = yaml.safe_load((GRAFANA / "provisioning" / "datasources" / "prometheus.yml").read_text(encoding="utf-8"))["datasources"][0]
    assert ds["uid"] == "omnivoice-prometheus" and ds["type"] == "prometheus" and ds["url"] == "http://prometheus:9090"
    prov = yaml.safe_load((GRAFANA / "provisioning" / "dashboards" / "omnivoice.yml").read_text(encoding="utf-8"))["providers"][0]
    assert prov["options"]["path"] == "/var/lib/grafana/dashboards"
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "./infrastructure/grafana/provisioning:/etc/grafana/provisioning:ro" in compose
    assert "./infrastructure/grafana/dashboards:/var/lib/grafana/dashboards:ro" in compose


# ---------- depuración de datos antes de enviar a Sentry ----------
def test_scrub_removes_credentials_bodies_numbers_and_transcripts():
    event = {
        "request": {"url": "https://api/x", "method": "POST", "data": {"to": "+34600000001", "text": "mi rut es 12.345.678-9"},
                    "cookies": {"s": "1"}, "query_string": "ticket=abc&key=sk-1",
                    "headers": {"Authorization": "Bearer eyJ...", "X-Twilio-Signature": "sig", "Content-Type": "application/json", "User-Agent": "x"}},
        "extra": {"phone": "+34600000001", "transcript": "hola", "tool": "check_inventory", "nested": {"api_key": "k", "ok": 1}},
        "user": {"id": "u1", "email": "a@b.cl", "ip_address": "1.2.3.4"},
        "breadcrumbs": {"values": [{"message": "x", "data": {"number": "+34600000001"}}]},
    }
    out = scrub_event(event)
    flat = json.dumps(out)
    for leaked in ("+34600000001", "12.345.678-9", "Bearer eyJ", "sk-1", "abc", "a@b.cl", "1.2.3.4", "sig"):
        assert leaked not in flat, leaked
    assert out["request"]["headers"]["Content-Type"] == "application/json"  # lo inocuo se conserva
    assert out["extra"]["tool"] == "check_inventory" and out["extra"]["nested"]["ok"] == 1 and out["extra"]["nested"]["api_key"] == "[filtrado]"
    assert out["user"] == {} and "data" not in out["request"] and "cookies" not in out["request"]


def test_scrub_is_bounded_on_deep_structures_and_tolerates_odd_events():
    deep = cur = {}
    for _ in range(50):
        cur["n"] = {}
        cur = cur["n"]
    assert "[recortado]" in json.dumps(scrub_event({"extra": deep}))
    assert scrub_event({}) == {} and scrub_event({"request": "no es dict", "breadcrumbs": None}) is not None


# ---------- activación opcional ----------
def settings(**kw):
    return Settings(environment="test", jwt_secret="t" * 40, **kw)


def test_disabled_by_default_and_without_libraries_it_warns_and_continues(monkeypatch):
    assert setup_sentry(settings()) is False and setup_tracing(object(), settings()) is False
    monkeypatch.setitem(sys.modules, "sentry_sdk", None)  # simula que no está instalada
    assert setup_sentry(settings(sentry_dsn="https://k@o.ingest.sentry.io/1")) is False
    for mod in ("opentelemetry", "opentelemetry.sdk.trace"):
        monkeypatch.setitem(sys.modules, mod, None)
    assert setup_tracing(object(), settings(otel_exporter_otlp_endpoint="http://collector:4318")) is False


def test_sentry_is_initialized_without_personal_data_and_with_the_scrubber(monkeypatch):
    calls = {}
    fake = types.ModuleType("sentry_sdk")
    fake.init = lambda **kw: calls.update(kw)
    monkeypatch.setitem(sys.modules, "sentry_sdk", fake)
    assert setup_sentry(settings(sentry_dsn="https://k@o.ingest.sentry.io/1", sentry_traces_sample_rate=0.1)) is True
    assert calls["send_default_pii"] is False and calls["before_send"] is telemetry.scrub_event
    assert calls["max_request_body_size"] == "never" and calls["environment"] == "test" and calls["traces_sample_rate"] == 0.1


def test_tracing_instruments_fastapi_without_noisy_probes(monkeypatch):
    seen = {}
    mods = {}

    def mk(name, **attrs):
        m = types.ModuleType(name)
        for k, v in attrs.items():
            setattr(m, k, v)
        mods[name] = m
        return m

    class Provider:
        def __init__(self, resource=None):
            seen["resource"] = resource
            self.processors = []

        def add_span_processor(self, p):
            self.processors.append(p)

    class Instrumentor:
        @staticmethod
        def instrument_app(app, excluded_urls=None):
            seen["app"], seen["excluded"] = app, excluded_urls

    trace = mk("opentelemetry.trace", set_tracer_provider=lambda p: seen.update(provider=p))
    root = mk("opentelemetry", trace=trace)
    mk("opentelemetry.exporter")
    mk("opentelemetry.exporter.otlp")
    mk("opentelemetry.exporter.otlp.proto")
    mk("opentelemetry.exporter.otlp.proto.http")
    mk("opentelemetry.exporter.otlp.proto.http.trace_exporter", OTLPSpanExporter=lambda endpoint: ("exporter", endpoint))
    mk("opentelemetry.instrumentation")
    mk("opentelemetry.instrumentation.fastapi", FastAPIInstrumentor=Instrumentor)
    mk("opentelemetry.sdk")
    mk("opentelemetry.sdk.resources", Resource=types.SimpleNamespace(create=lambda attrs: attrs))
    mk("opentelemetry.sdk.trace", TracerProvider=Provider)
    mk("opentelemetry.sdk.trace.export", BatchSpanProcessor=lambda exp: ("batch", exp))
    del root
    for name, m in mods.items():
        monkeypatch.setitem(sys.modules, name, m)
    app = object()
    assert setup_tracing(app, settings(otel_exporter_otlp_endpoint="http://collector:4318/", otel_service_name="ov")) is True
    assert seen["app"] is app and seen["excluded"] == "healthz,readyz,metrics"
    assert seen["resource"] == {"service.name": "ov", "deployment.environment": "test"}
    assert seen["provider"].processors == [("batch", ("exporter", "http://collector:4318/v1/traces"))]


@pytest.mark.parametrize("name", ["omnivoice.json"])
def test_dashboard_file_is_utf8_json(name):
    assert json.loads((GRAFANA / "dashboards" / name).read_bytes().decode("utf-8"))["title"].startswith("OmniVoice")
