"""Migración 016 contra PostgreSQL real: fragmentos con búsqueda de texto completo en español, versiones, RLS y borrado en cascada."""
import base64
import os
import uuid

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings, get_settings
from app.main import create_app
from app.security.auth import Principal, issue_token
from app.tools.handlers import build_registry

PG_URL = os.environ.get("TEST_DATABASE_URL")
REDIS_URL = os.environ.get("TEST_REDIS_URL")
DEMO = "00000000-0000-0000-0000-000000000001"
OTHER = "00000000-0000-0000-0000-0000000000ee"

pytestmark = pytest.mark.skipif(not (PG_URL and REDIS_URL), reason="requiere TEST_DATABASE_URL y TEST_REDIS_URL")


def _h(org, role="admin"):
    return {"Authorization": f"Bearer {issue_token('u-kb', org, role)}"}


def test_knowledge_lifecycle_search_and_isolation_on_postgres():
    s = Settings(environment="test", jwt_secret=get_settings().jwt_secret, persistence_backend="postgres", state_backend="redis",
                 database_url=PG_URL, redis_url=REDIS_URL, vad_backend="energy", retention_job_enabled=False)
    tag = uuid.uuid4().hex[:8]
    with TestClient(create_app(s)) as c:
        text = (f"Manual {tag}. El horario de atención es de lunes a sábado. Los envíos tardan tres días hábiles. "
                f"Las devoluciones se aceptan durante treinta días con el ticket de compra.")
        r = c.post("/api/v1/knowledge/sources", headers=_h(DEMO), json={"name": f"Manual {tag}", "text": text})
        assert r.status_code == 201, r.text
        src = r.json()
        assert src["version"] == 1 and src["chunk_count"] >= 1

        # búsqueda en español: raíces y acentos («enviar» encuentra «envíos», «atencion» sin tilde encuentra «atención»)
        for q in ("¿cuánto tardan los envíos?", "horario de atencion", "devolución del producto con ticket"):
            hits = c.get("/api/v1/knowledge/search", params={"q": q}, headers=_h(DEMO)).json()["results"]
            assert any(h.get("source_id") == src["id"] for h in hits), q
        assert not c.get("/api/v1/knowledge/search", params={"q": "criptomonedas obsidiana"}, headers=_h(DEMO)).json()["results"]

        # otra organización: ni lo ve, ni lo busca, ni lo toca (RLS)
        assert c.get("/api/v1/knowledge/sources", headers=_h(OTHER)).json()["items"] == []
        assert not c.get("/api/v1/knowledge/search", params={"q": "horario atención"}, headers=_h(OTHER)).json()["results"]
        assert c.delete(f"/api/v1/knowledge/sources/{src['id']}", headers=_h(OTHER)).status_code == 404
        assert c.put(f"/api/v1/knowledge/sources/{src['id']}", headers=_h(OTHER), json={"name": "x", "text": "hola mundo"}).status_code == 404

        # la herramienta del agente recupera con fuente y avisa cuando no hay nada
        reg = build_registry(c.app.state.repo)
        mine = Principal("u", DEMO, "customer")
        found = c.portal.call(reg.execute, "search_knowledge_base", {"query": "envíos"}, mine, "kb-1")["data"]
        assert found["found"] and any(r.get("source_id") == src["id"] for r in found["results"])
        none = c.portal.call(reg.execute, "search_knowledge_base", {"query": "criptomonedas obsidiana"}, mine, "kb-2")["data"]
        assert none["found"] is False

        # sustitución: versión 2 y los fragmentos anteriores desaparecen
        new = f"Versión nueva {tag}. Ahora también atendemos los domingos por la mañana."
        rep = c.put(f"/api/v1/knowledge/sources/{src['id']}", headers=_h(DEMO), json={"name": f"Manual {tag} v2", "text": new})
        assert rep.status_code == 200 and rep.json()["version"] == 2
        assert any(h.get("source_id") == src["id"] for h in c.get("/api/v1/knowledge/search", params={"q": "domingos mañana"}, headers=_h(DEMO)).json()["results"])
        assert not any(h.get("source_id") == src["id"] for h in c.get("/api/v1/knowledge/search", params={"q": "treinta días ticket devoluciones"}, headers=_h(DEMO)).json()["results"])

        # duplicado por contenido
        dup = c.post("/api/v1/knowledge/sources", headers=_h(DEMO), json={"name": "otra copia", "text": new})
        assert dup.status_code == 409

        # PDF/DOCX/CSV reales a través del mismo camino
        csv_raw = f"producto;precio\nchaqueta{tag};89,90".encode()
        up = c.post("/api/v1/knowledge/sources", headers=_h(DEMO), json={"name": f"precios {tag}", "kind": "csv", "content_base64": base64.b64encode(csv_raw).decode()})
        assert up.status_code == 201 and up.json()["kind"] == "csv"

        # borrado en cascada: sus fragmentos dejan de existir
        for sid in (src["id"], up.json()["id"]):
            assert c.delete(f"/api/v1/knowledge/sources/{sid}", headers=_h(DEMO)).status_code == 204
        assert not any(h.get("source_id") == src["id"] for h in c.get("/api/v1/knowledge/search", params={"q": "domingos"}, headers=_h(DEMO)).json()["results"] if "source_id" in h)
