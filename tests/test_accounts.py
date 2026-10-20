import time

from fastapi.testclient import TestClient
from sqlalchemy import select

from test_public_access import public_client, basic
from tunnel_qa.auth.models import LoginSession, UserAccount, AccountEvent
from tunnel_qa.auth.service import bootstrap, COOKIE


def login(client, username="public-user", password="strong-public-password"):
    result = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert result.status_code == 200, result.text
    return {"X-CSRF-Token": result.json()["csrf_token"]}


def values(**updates):
    result = {"username": "new-reader", "display_name": "测试成员", "role": "field_engineer",
              "enabled": True, "password": "reader-password-123", "admin_password": "strong-public-password"}
    return {**result, **updates}


def test_login_logout_and_browser_does_not_reuse_basic(tmp_path):
    client = public_client(tmp_path)
    assert client.get("/login").status_code == 200
    result = client.get("/", headers={"Sec-Fetch-Mode": "navigate", **basic("public-user", "strong-public-password")}, follow_redirects=False)
    assert result.status_code == 303 and "WWW-Authenticate" not in result.headers
    csrf = login(client)
    assert client.cookies.get(COOKIE)
    assert client.get("/api/v1/auth/me").json()["can_manage"]
    assert client.post("/api/v1/auth/logout").status_code == 403
    assert client.post("/api/v1/auth/logout", headers=csrf).status_code == 200
    assert client.get("/api/v1/auth/me").status_code == 401


def test_cookie_flags_and_expiry(tmp_path):
    client = public_client(tmp_path)
    with TestClient(client.app, base_url="https://testserver") as secure:
        response = secure.post("/api/v1/auth/login", json={"username": "public-user", "password": "strong-public-password"})
        cookie = response.headers["set-cookie"].lower()
        assert "httponly" in cookie and "secure" in cookie and "samesite=lax" in cookie
        with client.app.state.database.session_factory() as session:
            active = session.scalar(select(LoginSession))
            active.expires_at = time.time() - 1
            session.commit()
        assert secure.get("/api/v1/auth/me").status_code == 401


def test_ordinary_account_cannot_manage_and_csrf_origin_is_enforced(tmp_path):
    client = public_client(tmp_path, public_reader_password="reader-password")
    csrf = login(client, "tunnel-reader", "reader-password")
    assert client.get("/accounts").status_code == 403
    assert client.get("/api/v1/accounts").status_code == 403
    assert client.post("/api/v1/accounts", headers=csrf, json=values()).status_code == 403
    assert client.get("/api/v1/settings", headers={"X-Role": "system_admin"}).status_code == 403
    assert client.post("/api/v1/auth/logout", headers={**csrf, "Origin": "https://evil.invalid"}).status_code == 403


def test_create_update_disable_and_revoke_sessions(tmp_path):
    admin = public_client(tmp_path)
    csrf = login(admin)
    created = admin.post("/api/v1/accounts", headers=csrf, json=values())
    assert created.status_code == 201
    reader_id = created.json()["id"]
    assert "password" not in created.text.replace('password_configured', '')
    assert admin.post("/api/v1/accounts", headers=csrf, json=values()).status_code == 409
    reader = TestClient(admin.app)
    login(reader, "new-reader", "reader-password-123")
    edited = admin.put(f"/api/v1/accounts/{reader_id}", headers=csrf, json=values(password="new-password-456"))
    assert edited.status_code == 200
    assert reader.get("/api/v1/auth/me").status_code == 401
    login(reader, "new-reader", "new-password-456")
    assert admin.put(f"/api/v1/accounts/{reader_id}", headers=csrf, json=values(password=None, enabled=False)).status_code == 200
    assert reader.get("/api/v1/auth/me").status_code == 401
    assert reader.post("/api/v1/auth/login", json={"username": "new-reader", "password": "new-password-456"}).status_code == 401
    with admin.app.state.database.session_factory() as session:
        account = session.get(UserAccount, reader_id)
        assert account.password_hash.startswith("pbkdf2_sha256$")
        assert "new-password-456" not in account.password_hash
        assert len(session.scalars(select(AccountEvent)).all()) == 3


def test_admin_password_change_persists_and_cannot_disable_self(tmp_path):
    client = public_client(tmp_path)
    csrf = login(client)
    mine = client.get("/api/v1/auth/me").json()["user_id"]
    data = values(username="public-user", role="system_admin", password=None)
    for update in ({"enabled": False}, {"role": "field_engineer"}):
        assert client.put(f"/api/v1/accounts/{mine}", headers=csrf, json={**data, **update}).status_code == 400
    response = client.put(f"/api/v1/accounts/{mine}", headers=csrf, json={**data, "password": "changed-admin-password"})
    assert response.status_code == 200 and response.json()["relogin_required"]
    assert client.get("/api/v1/auth/me").status_code == 401
    with client.app.state.database.session_factory() as session:
        bootstrap(session, client.app.state.settings)
    login(client, password="changed-admin-password")
    assert client.post("/api/v1/auth/login", json={"username": "public-user", "password": "strong-public-password"}).status_code == 401


def test_pending_account_activation_validation_and_secret_redaction(tmp_path):
    client = public_client(tmp_path)
    csrf = login(client)
    pending = next(a for a in client.get("/api/v1/accounts").json()["accounts"] if not a["password_configured"])
    assert not pending["enabled"]
    data = values(username=pending["username"], password=None)
    assert client.put(f'/api/v1/accounts/{pending["id"]}', headers=csrf, json=data).status_code == 400
    invalid = client.post("/api/v1/accounts", headers=csrf, json=values(password="SECRET"))
    assert invalid.status_code == 422 and "SECRET" not in invalid.text
    assert client.post("/api/v1/accounts", headers=csrf, json=values(admin_password="wrong")).status_code == 403
    assert client.post("/api/v1/accounts", json=values()).status_code == 403


def test_login_throttling_and_cross_origin_login(tmp_path):
    client = public_client(tmp_path)
    data = {"username": "public-user", "password": "wrong"}
    assert client.post("/api/v1/auth/login", json=data, headers={"Origin": "https://evil.invalid"}).status_code == 403
    for _ in range(10):
        assert client.post("/api/v1/auth/login", json=data).status_code == 401
    assert client.post("/api/v1/auth/login", json=data).status_code == 429
