import base64

import pytest
from fastapi.testclient import TestClient

from tunnel_qa.app import create_app
from tunnel_qa.core.config import Settings
from tunnel_qa.db.session import Database
from tunnel_qa.qa.model_gateway import ModelOutput


class PublicAccessGateway:
    provider = "test"
    model = "test"

    def generate(self, *, question, evidence, scenario, response_mode="detailed"):
        return ModelOutput(answer="test", follow_up=[])


def public_client(tmp_path, **overrides):
    settings = Settings(
        _env_file=None,
        database_url="sqlite:///:memory:",
        original_content_database_url=f"sqlite:///{tmp_path / 'original_documents.db'}",
        chunk_content_database_url=f"sqlite:///{tmp_path / 'document_chunks.db'}",
        storage_path=tmp_path / "storage",
        public_access_enabled=True,
        public_access_username="public-user",
        public_access_password="strong-public-password",
        environment="production",
        **overrides,
    )
    database = Database(settings)
    app = create_app(settings, database, PublicAccessGateway())
    return TestClient(app)


def basic(username: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def test_public_access_requires_credentials_and_allows_health(tmp_path):
    client = public_client(tmp_path)

    denied_page = client.get("/", follow_redirects=False)
    denied_api = client.get("/api/v1/settings")
    health = client.get("/healthz")

    assert denied_page.status_code == 303
    assert denied_page.headers["location"] == "/login"
    assert denied_page.headers["WWW-Authenticate"].startswith("Basic")
    assert denied_api.status_code == 401
    assert denied_api.json()["error_code"] == "PUBLIC_AUTH_REQUIRED"
    assert health.status_code == 200


def test_public_access_accepts_valid_credentials_and_sets_security_headers(tmp_path):
    client = public_client(tmp_path)
    response = client.get(
        "/",
        headers={
            **basic("public-user", "strong-public-password"),
            "Accept-Encoding": "gzip",
        },
    )

    assert response.status_code == 200
    assert response.headers["Strict-Transport-Security"].startswith("max-age=")
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    assert response.headers["Content-Encoding"] == "gzip"


def test_public_access_rejects_wrong_password(tmp_path):
    client = public_client(tmp_path)
    response = client.get("/api/v1/auth/me", headers=basic("public-user", "wrong-password"))
    assert response.status_code == 401


def test_public_identity_is_bound_to_login_not_forged_headers(tmp_path):
    client = public_client(tmp_path, public_reader_password="reader-test-password")
    forged = {**basic("tunnel-reader", "reader-test-password"),
              "X-User-Id": "public-user", "X-Role": "system_admin",
              "X-Security-Level": "confidential"}
    identity = client.get("/api/v1/auth/me", headers=forged).json()
    assert identity == {"user_id": "tunnel-reader", "role": "field_engineer",
                        "security_level": "internal", "can_manage": False,
                        "username": "tunnel-reader", "csrf_token": None}
    assert client.get("/api/v1/settings", headers=forged).status_code == 403
    assert client.delete("/api/v1/data/documents/nonexistent", headers=forged).status_code == 403
    assert client.get("/api/v1/graph/workspace", headers=forged).status_code == 200
    admin = client.get("/api/v1/auth/me", headers=basic("public-user", "strong-public-password"))
    assert admin.json()["can_manage"] is True
    assert client.get("/api/v1/settings", headers=basic("public-user", "strong-public-password")).status_code == 200


def test_reader_cannot_login_until_password_is_configured(tmp_path):
    client = public_client(tmp_path)
    assert client.get("/api/v1/auth/me", headers=basic("tunnel-reader", "")).status_code == 401


def test_failed_login_is_rate_limited_without_locking_valid_admin(tmp_path):
    client = public_client(tmp_path)
    for _ in range(20):
        assert client.get("/api/v1/auth/me", headers=basic("public-user", "wrong")).status_code == 401
    assert client.get("/api/v1/auth/me", headers=basic("public-user", "wrong")).status_code == 429
    assert client.get("/", headers=basic("public-user", "strong-public-password")).status_code == 200


def test_public_accounts_cannot_have_identical_names(tmp_path):
    with pytest.raises(RuntimeError, match="同名"):
        public_client(tmp_path, public_reader_username="public-user", public_reader_password="reader-password")


@pytest.mark.parametrize(
    "username,password",
    [("错误用户", "wrong"), ("public-user", "错误密码")],
)
def test_public_access_handles_unicode_invalid_credentials(tmp_path, username, password):
    client = public_client(tmp_path)
    response = client.get("/api/v1/auth/me", headers=basic(username, password))
    assert response.status_code == 401


def test_public_access_requires_password_when_enabled(tmp_path):
    settings = Settings(
        _env_file=None,
        database_url="sqlite:///:memory:",
        original_content_database_url=f"sqlite:///{tmp_path / 'original_documents.db'}",
        chunk_content_database_url=f"sqlite:///{tmp_path / 'document_chunks.db'}",
        storage_path=tmp_path / "storage",
        public_access_enabled=True,
        public_access_password=None,
    )
    database = Database(settings)
    with pytest.raises(RuntimeError, match="PUBLIC_ACCESS_PASSWORD"):
        create_app(settings, database, PublicAccessGateway())
