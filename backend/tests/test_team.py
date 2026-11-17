"""Equipo: miembros y roles con reglas de negocio (solo owner toca owners, nunca sin owner, claves de API no gestionan personas)."""
import uuid

from app.security.auth import issue_token
from tests.test_api import make_client

ORG = str(uuid.uuid4())


def h(role, org=ORG):
    return {"Authorization": f"Bearer {issue_token('u-team', org, role)}"}


def test_team_lifecycle_and_owner_rules():
    c = make_client()
    assert c.get("/api/v1/team", headers=h("admin")).json() == {"items": []}

    # un admin puede invitar roles no-owner, pero no crear owners
    r = c.put("/api/v1/team/ana@example.com", headers=h("admin"), json={"role": "operator"})
    assert r.status_code == 200 and r.json()["previous"] is None
    assert c.put("/api/v1/team/ana@example.com", headers=h("admin"), json={"role": "owner"}).status_code == 403

    # el owner crea el primer owner; entonces ese owner no puede degradarse ni borrarse (último owner)
    assert c.put("/api/v1/team/jefe@example.com", headers=h("owner"), json={"role": "owner"}).status_code == 200
    assert c.put("/api/v1/team/jefe@example.com", headers=h("owner"), json={"role": "admin"}).status_code == 409
    assert c.delete("/api/v1/team/jefe@example.com", headers=h("owner")).status_code == 409
    # con un segundo owner sí se puede, pero un admin nunca toca a un owner
    assert c.put("/api/v1/team/otro@example.com", headers=h("owner"), json={"role": "owner"}).status_code == 200
    assert c.delete("/api/v1/team/jefe@example.com", headers=h("admin")).status_code == 403
    assert c.delete("/api/v1/team/jefe@example.com", headers=h("owner")).status_code == 204
    assert c.delete("/api/v1/team/jefe@example.com", headers=h("owner")).status_code == 404

    emails = [m["email"] for m in c.get("/api/v1/team", headers=h("admin")).json()["items"]]
    assert emails == ["ana@example.com", "otro@example.com"]


def test_team_validation_permissions_and_isolation():
    c = make_client()
    assert c.put("/api/v1/team/no-es-email", headers=h("admin"), json={"role": "viewer"}).status_code == 422
    assert c.put("/api/v1/team/a@example.com", headers=h("admin"), json={"role": "superadmin"}).status_code == 422
    assert c.put("/api/v1/team/a@example.com", headers=h("operator"), json={"role": "viewer"}).status_code == 403
    assert c.get("/api/v1/team").status_code in (401, 403)
    c.put("/api/v1/team/a@example.com", headers=h("admin"), json={"role": "viewer"})
    other = str(uuid.uuid4())
    assert c.get("/api/v1/team", headers=h("admin", other)).json() == {"items": []}
