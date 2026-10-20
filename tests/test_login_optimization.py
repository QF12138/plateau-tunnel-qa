"""Login recovery, safe navigation and session security regressions (isolated DB)."""
import hashlib
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from sqlalchemy import select

from test_public_access import public_client
from tunnel_qa.auth import service
from tunnel_qa.auth.models import LoginSession
from tunnel_qa.auth.service import COOKIE, LoginLimiter, require_same_origin, safe_login_destination
from tunnel_qa.core.errors import AppError


def credentials(**changes):
    return {"username": "public-user", "password": "strong-public-password", **changes}


@pytest.mark.parametrize("origin,allowed", [
    ("https://testserver", True), ("https://TESTSERVER:443", True),
    ("http://testserver", False), ("https://testserver:444", False),
    ("https://testserver:0", False), ("https://evil.invalid", False),
    ("null", False), ("https://testserver@evil.invalid", False),
    ("https://testserver/path", False), ("https://testserver#fragment", False),
])
def test_origin_includes_scheme_host_and_effective_port(origin, allowed):
    request = Request({"type": "http", "scheme": "https", "path": "/api/v1/auth/login",
                       "headers": [(b"host", b"testserver"), (b"origin", origin.encode())]})
    if allowed:
        require_same_origin(request)
    else:
        with pytest.raises(AppError) as error:
            require_same_origin(request)
        assert error.value.status_code == 403


@pytest.mark.parametrize("target", [None, "https://evil.invalid", "//evil.invalid", "/\\evil.invalid",
                                     "%2F%2Fevil.invalid", "/%2f%2fevil.invalid", "/accounts?next=//evil.invalid",
                                     "/accounts\n", " /accounts", "/api/docs"])
def test_only_exact_allowed_login_destinations(target):
    assert safe_login_destination(target, can_manage=True) == "/"
    assert safe_login_destination("/accounts", can_manage=False) == "/"
    assert safe_login_destination("/accounts", can_manage=True) == "/accounts"


def test_login_returns_to_accounts_and_authenticated_login_page_redirects(tmp_path):
    client = public_client(tmp_path, public_reader_password="reader-password")
    response = client.get("/accounts", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=%2Faccounts"
    result = client.post("/api/v1/auth/login", json=credentials(next="/accounts"))
    assert result.status_code == 200 and result.json()["redirect_to"] == "/accounts"
    assert client.get("/login?next=%2Faccounts", follow_redirects=False).headers["location"] == "/accounts"
    assert client.get("/login?next=//evil.invalid", follow_redirects=False).headers["location"] == "/"
    reader = TestClient(client.app)
    result = reader.post("/api/v1/auth/login", json=credentials(username="tunnel-reader", password="reader-password", next="/accounts"))
    assert result.json()["redirect_to"] == "/"
    assert reader.get("/login?next=%2Faccounts", follow_redirects=False).headers["location"] == "/"
    assert reader.get("/accounts").status_code == 403


def test_login_rotates_only_current_browser_and_failed_login_preserves_it(tmp_path):
    client = public_client(tmp_path)
    assert client.post("/api/v1/auth/login", json=credentials()).status_code == 200
    old = client.cookies.get(COOKIE)
    other = TestClient(client.app)
    assert other.post("/api/v1/auth/login", json=credentials()).status_code == 200
    assert client.post("/api/v1/auth/login", json=credentials(password="wrong")).status_code == 401
    assert client.get("/api/v1/auth/me").status_code == 200
    response = client.post("/api/v1/auth/login", json=credentials())
    assert response.status_code == 200 and client.cookies.get(COOKIE) != old
    assert client.get("/api/v1/auth/me").status_code == 200
    assert other.get("/api/v1/auth/me").status_code == 200
    with client.app.state.database.session_factory() as session:
        assert session.get(LoginSession, hashlib.sha256(old.encode()).hexdigest()) is None
        assert len(session.scalars(select(LoginSession)).all()) == 2
    stale = TestClient(client.app)
    stale.cookies.set(COOKIE, old)
    assert stale.get("/api/v1/auth/me").status_code == 401


@pytest.mark.parametrize("path", ["/api/v1/auth/me", "/", "/accounts", "/login?next=%2Faccounts"])
def test_invalid_cookie_is_cleared_and_page_redirect_explains_expiry(tmp_path, path):
    client = public_client(tmp_path)
    client.cookies.set(COOKIE, "expired-or-revoked-token")
    response = client.get(path, follow_redirects=False)
    assert "Max-Age=0" in response.headers["set-cookie"]
    if path.startswith("/api/"):
        assert response.status_code == 401
    else:
        assert response.status_code == 303
        target = urlsplit(response.headers["location"])
        assert target.path == "/login"
        assert parse_qs(target.query)["reason"] == ["expired"]
        if "accounts" in path:
            assert parse_qs(target.query)["next"] == ["/accounts"]


def test_throttle_countdown_prunes_each_entry_and_scopes_confirmation(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(service, "time", SimpleNamespace(monotonic=lambda: now[0]))
    limiter = LoginLimiter()
    for _ in range(10):
        limiter.failed("USER")
    with pytest.raises(AppError) as first:
        limiter.check(" user ")
    assert first.value.details["retry_after_seconds"] == 60
    limiter.check("user", scope="admin-confirmation")
    now[0] += 41
    with pytest.raises(AppError) as second:
        limiter.check("user")
    assert second.value.details["retry_after_seconds"] == 19
    now[0] += 19
    limiter.check("user")
    assert not limiter.failures
    for i in range(10):
        now[0] = 200 + i * 7
        limiter.failed("window-user")
    assert len(limiter.failures[("login", "window-user")]) == 9
    limiter.succeeded("WINDOW-USER")
    assert not limiter.failures


def test_global_throttle_expires_and_does_not_cross_admin_scope(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(service, "time", SimpleNamespace(monotonic=lambda: now[0]))
    limiter = LoginLimiter()
    for i in range(100):
        limiter.failed(f"user-{i}")
    with pytest.raises(AppError):
        limiter.check("new-user")
    limiter.check("admin-id", scope="admin-confirmation")
    now[0] = 160
    limiter.check("new-user")
    assert not limiter.failures


def test_retry_after_header_and_success_clears_old_failures(tmp_path):
    client = public_client(tmp_path)
    limiter = client.app.state.login_limiter
    for _ in range(9):
        limiter.failed("public-user")
    result = client.post("/api/v1/auth/login", json=credentials(next="//evil.invalid"))
    assert result.status_code == 200 and result.json()["redirect_to"] == "/"
    assert ("login", "public-user") not in limiter.failures
    for _ in range(10):
        limiter.failed("public-user")
    blocked = client.post("/api/v1/auth/login", json=credentials())
    assert blocked.status_code == 429
    assert 1 <= int(blocked.headers["Retry-After"]) <= 60
    assert blocked.json()["details"]["retry_after_seconds"] == int(blocked.headers["Retry-After"])


def test_https_same_origin_login_remains_usable(tmp_path):
    client = public_client(tmp_path)
    secure = TestClient(client.app, base_url="https://testserver")
    assert secure.post("/api/v1/auth/login", json=credentials(), headers={"Origin": "http://testserver"}).status_code == 403
    result = secure.post("/api/v1/auth/login", json=credentials(), headers={"Origin": "https://testserver:443"})
    assert result.status_code == 200
    assert "Secure" in result.headers["set-cookie"]
