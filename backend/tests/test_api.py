"""Tests de API y WebSocket con FastAPI TestClient (proveedor simulado, estado en memoria)."""
import struct

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.core.config import Settings
from app.main import create_app
from app.realtime.provider import FakeProvider
from app.security.auth import issue_token

LOUD = struct.pack("<480h", *([12000, -12000] * 240))


def frame(seq: int, pcm: bytes = LOUD) -> bytes:
    return struct.pack(">I", seq) + pcm


def make_client(**overrides):
    settings = Settings(environment="test", jwt_secret="t" * 40, **{"vad_backend": "energy", **overrides})
    app = create_app(settings, provider_factory=lambda: FakeProvider(audio_chunks=5, respond_on_audio=True))
    return TestClient(app)


def auth(role="customer", org="o1", user="u1"):
    return {"Authorization": f"Bearer {issue_token(user, org, role)}"}


def open_ws(c, headers, agent_id=None):
    body = {"agent_id": agent_id} if agent_id else {}
    sid = c.post("/api/v1/sessions", json=body, headers=headers).json()["session_id"]
    conn = c.post(f"/api/v1/sessions/{sid}/connect", headers=headers).json()
    return sid, conn


def test_health_and_ready():
    c = make_client()
    assert c.get("/healthz").json() == {"status": "ok"}
    assert c.get("/readyz").status_code == 200


def test_requires_auth_and_roles():
    c = make_client()
    assert c.post("/api/v1/sessions").status_code == 401
    assert c.get("/api/v1/audit-logs", headers=auth("customer")).status_code == 403
    assert c.get("/api/v1/audit-logs", headers=auth("admin")).status_code == 200
    assert c.post("/api/v1/agents", headers=auth("operator"), json={}).status_code == 403


def test_security_headers_and_request_id():
    c = make_client()
    r = c.get("/healthz", headers={"X-Request-ID": "abc123"})
    assert r.headers["X-Request-ID"] == "abc123" and r.headers["X-Content-Type-Options"] == "nosniff"


def test_full_voice_flow_streams_audio_and_persists_conversation():
    c = make_client()
    h = auth()
    _, conn = open_ws(c, h)
    assert conn["audio"]["sample_rate"] == 24000
    with c.websocket_connect(conn["ws_path"]) as ws:
        assert ws.receive_json()["type"] == "session.ready"
        ws.send_bytes(frame(0))
        got_audio = False
        for _ in range(30):
            m = ws.receive()
            if m.get("bytes"):
                got_audio = True
                break
        assert got_audio
        ws.send_text('{"type":"end"}')
    convs = c.get("/api/v1/conversations", headers=auth("operator")).json()["items"]
    assert len(convs) == 1 and convs[0]["final_state"] == "COMPLETED"
    detail = c.get(f"/api/v1/conversations/{convs[0]['id']}", headers=auth("operator"))
    assert detail.status_code == 200


def test_ticket_cannot_be_reused_and_bad_ticket_is_rejected():
    c = make_client()
    _, conn = open_ws(c, auth())
    with c.websocket_connect(conn["ws_path"]) as ws:
        ws.receive_json()
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect(conn["ws_path"]):
            pass
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect("/ws/audio?ticket=inventado"):
            pass


def test_connect_is_bound_to_the_user_who_created_the_session():
    c = make_client()
    sid = c.post("/api/v1/sessions", json={}, headers=auth(user="alice")).json()["session_id"]
    assert c.post(f"/api/v1/sessions/{sid}/connect", headers=auth(user="mallory")).status_code == 404


def test_conversations_are_isolated_between_organizations():
    c = make_client()
    _, conn = open_ws(c, auth(org="o1"))
    with c.websocket_connect(conn["ws_path"]) as ws:
        ws.receive_json()
        ws.send_text('{"type":"end"}')
    assert len(c.get("/api/v1/conversations", headers=auth("operator", org="o1")).json()["items"]) == 1
    assert c.get("/api/v1/conversations", headers=auth("operator", org="o2")).json()["items"] == []


def test_org_session_cap_returns_429():
    c = make_client(max_sessions_per_org=1)
    _, conn = open_ws(c, auth())
    with c.websocket_connect(conn["ws_path"]) as ws:
        ws.receive_json()
        assert c.post("/api/v1/sessions", json={}, headers=auth(user="u2")).status_code == 429


def test_session_creation_rate_limit():
    c = make_client(rate_limit_sessions_per_min=2)
    codes = [c.post("/api/v1/sessions", json={}, headers=auth()).status_code for _ in range(4)]
    assert codes == [200, 200, 429, 429]


def test_agents_validate_tools_and_are_used():
    c = make_client()
    admin = auth("admin")
    bad = c.post("/api/v1/agents", headers=admin, json={"name": "Bot", "instructions": "x" * 20, "tools": ["drop_tables"]})
    assert bad.status_code == 422
    ok = c.post("/api/v1/agents", headers=admin, json={"name": "Bot", "instructions": "Atiende reservas.", "tools": ["check_reservation"]})
    assert ok.status_code == 201
    listed = c.get("/api/v1/agents", headers=auth("operator")).json()
    assert listed["items"][0]["tools"] == ["check_reservation"]
    assert c.post("/api/v1/sessions", json={"agent_id": "no-existe"}, headers=auth()).status_code == 404


def test_rejects_unknown_fields_in_bodies():
    c = make_client()
    r = c.post("/api/v1/agents", headers=auth("admin"),
               json={"name": "Bot", "instructions": "x" * 20, "tools": [], "is_admin": True})
    assert r.status_code == 422


def test_metrics_endpoint_reports_percentiles_shape():
    c = make_client()
    m = c.get("/api/v1/metrics", headers=auth("operator")).json()
    assert set(m["first_audio_ms"]) == {"count", "p50", "p95", "p99"}
