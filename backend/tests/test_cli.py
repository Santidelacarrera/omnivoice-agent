import jwt

from app.cli import main
from app.core.config import get_settings

ORG = "00000000-0000-0000-0000-000000000001"


def test_issue_token_roundtrip(capsys):
    assert main(["issue-token", "--org", ORG, "--user", "u1", "--role", "admin", "--ttl", "120"]) == 0
    tok = capsys.readouterr().out.strip()
    data = jwt.decode(tok, get_settings().jwt_secret, algorithms=["HS256"])
    assert data["org"] == ORG and data["role"] == "admin" and data["sub"] == "u1"


def test_issue_token_rejects_bad_org_and_ttl():
    assert main(["issue-token", "--org", "x", "--user", "u"]) == 2
    assert main(["issue-token", "--org", ORG, "--user", "u", "--ttl", "999999"]) == 2
