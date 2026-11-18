"""Exposición en producción: ni el esquema OpenAPI ni las páginas de documentación ni el token de desarrollo se publican."""
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app


def _client(env):
    extra = ({"persistence_backend": "postgres", "state_backend": "redis", "database_url": "postgresql+asyncpg://u:p@localhost:1/x",
              "redis_url": "redis://localhost:1/0", "cors_origins": ["https://app.example.com"], "jwt_secret": "p" * 48}
             if env == "production" else {"jwt_secret": "t" * 40})
    return TestClient(create_app(Settings(environment=env, vad_backend="energy", **extra)))  # sin lifespan: solo se prueban las rutas


def test_production_hides_api_schema_docs_and_dev_token():
    c = _client("production")
    for path in ("/openapi.json", "/docs", "/redoc"):
        assert c.get(path).status_code == 404, path
    assert c.post("/api/v1/auth/dev-token", json={}).status_code == 404


def test_development_keeps_docs_available():
    c = _client("test")
    assert c.get("/openapi.json").status_code == 200 and c.get("/docs").status_code == 200


def test_control_characters_in_path_params_never_reach_the_database():
    """Un `%00` en el nombre o el email rompía asyncpg (500) en PostgreSQL: ahora se rechaza antes."""
    import uuid

    from app.security.auth import issue_token
    from tests.test_api import make_client

    c = make_client()
    h = {"Authorization": f"Bearer {issue_token('u', str(uuid.uuid4()), 'owner')}"}
    assert c.patch("/api/v1/integrations/%00", json={"enabled": False}, headers=h).status_code == 404
    assert c.delete("/api/v1/integrations/%00", headers=h).status_code == 404
    assert c.post("/api/v1/integrations/%00/test", json={"path": "/"}, headers=h).status_code == 404
    assert c.delete("/api/v1/team/a%00@example.com", headers=h).status_code == 422
    assert c.put("/api/v1/team/a%00@example.com", json={"role": "viewer"}, headers=h).status_code == 422
