"""Extremo a extremo con PostgreSQL y Redis reales: API + WebSocket + herramienta + persistencia + supresión."""
import json
import os
import struct
import time

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings, get_settings
from app.main import create_app
from app.realtime.provider import FakeProvider
from app.security.auth import issue_token

PG_URL = os.environ.get("TEST_DATABASE_URL")
REDIS_URL = os.environ.get("TEST_REDIS_URL")
DEMO = "00000000-0000-0000-0000-000000000001"
LOUD = struct.pack("<480h", *([12000, -12000] * 240))

pytestmark = pytest.mark.skipif(not (PG_URL and REDIS_URL), reason="requiere TEST_DATABASE_URL y TEST_REDIS_URL")


def _provider():
    return FakeProvider(audio_chunks=3, respond_on_audio=True, script=[
        {"type": "transcript_user", "text": "¿hay chaquetas negras talla M?"},
        {"type": "tool_call", "call_id": "call-1", "name": "check_inventory",
         "arguments": json.dumps({"product": "chaqueta", "color": "negro", "size": "M"})},
    ])


def _headers(role="admin"):
    return {"Authorization": f"Bearer {issue_token('u-e2e', DEMO, role)}"}


def test_full_conversation_roundtrip_on_real_services():
    settings = Settings(environment="test", jwt_secret=get_settings().jwt_secret, persistence_backend="postgres",
                        state_backend="redis", database_url=PG_URL, redis_url=REDIS_URL, vad_backend="energy",
                        retention_job_enabled=False)
    with TestClient(create_app(settings, provider_factory=_provider)) as c:
        assert c.get("/readyz").json()["status"] == "ready"

        sid = c.post("/api/v1/sessions", json={}, headers=_headers()).json()["session_id"]
        conn = c.post(f"/api/v1/sessions/{sid}/connect", headers=_headers()).json()
        with c.websocket_connect(conn["ws_path"]) as ws:
            assert ws.receive_json()["type"] == "session.ready"
            for i in range(6):
                ws.send_bytes(struct.pack(">I", i) + LOUD)
            seen_tool_end = False
            for _ in range(80):
                m = ws.receive()
                if m.get("text") and '"tool.end"' in m["text"]:
                    seen_tool_end = '"ok": true' in m["text"]
                    break
            assert seen_tool_end, "la herramienta debe ejecutarse con datos reales de PostgreSQL"
            ws.send_text('{"type":"end"}')
            for _ in range(200):
                m = ws.receive()
                if m.get("text") and '"session.closed"' in m["text"]:
                    break

        items = []
        for _ in range(100):
            items = c.get("/api/v1/conversations", headers=_headers()).json()["items"]
            if items:
                break
            time.sleep(0.05)
        assert items, "la conversación debe quedar guardada en PostgreSQL"
        cid = items[0]["id"]
        detail = c.get(f"/api/v1/conversations/{cid}", headers=_headers()).json()
        assert any(t["tool_name"] == "check_inventory" and t["ok"] for t in detail["tools"])

        assert c.delete(f"/api/v1/conversations/{cid}", headers=_headers("operator")).status_code == 403
        assert c.delete(f"/api/v1/conversations/{cid}", headers=_headers()).status_code == 204
        assert c.get(f"/api/v1/conversations/{cid}", headers=_headers()).status_code == 404
        actions = [a["action"] for a in c.get("/api/v1/audit-logs", headers=_headers()).json()["items"]]
        assert "conversation.deleted" in actions
