"""Public launch checks. Output contains statuses only, never credentials."""
import argparse
import json
import sqlite3
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from tunnel_qa.core.config import Settings


def configuration_ready(settings: Settings) -> bool:
    password = settings.public_access_password
    reader = settings.public_reader_password
    return bool(settings.public_access_enabled and password and password.get_secret_value().strip()
                and (reader is None or (reader.get_secret_value().strip()
                     and settings.public_reader_username != settings.public_access_username)))


def probe(base_url: str, settings: Settings, client: httpx.Client, *, verify_admin: bool = True) -> dict:
    parsed = urlsplit(base_url)
    if (parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"})) or parsed.username or parsed.password or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        return {"ready": False, "error": "UNSAFE_PROBE_URL"}
    if not configuration_ready(settings):
        return {"ready": False, "error": "PUBLIC_AUTH_NOT_CONFIGURED"}
    result = {"ready": False}
    try:
        base_url = base_url.rstrip("/")
        health = client.get(base_url + "/healthz")
        result["health_status"] = health.status_code
        denied = client.get(base_url + "/api/v1/auth/me")
        result["anonymous_status"] = denied.status_code
        if health.status_code != 200 or health.json().get("status") != "ok":
            result["error"] = "HEALTH_CHECK_FAILED"
            return result
        if denied.status_code != 401 or not denied.headers.get("www-authenticate", "").startswith("Basic"):
            result["error"] = "AUTH_GATE_CHECK_FAILED"
            return result
        if not verify_admin:
            login_page = client.get(base_url + "/login")
            result["ready"] = login_page.status_code == 200 and 'id="login-form"' in login_page.text
            result["admin_login_verified"] = False
            result["note"] = "Account managed in UI; only login page and anonymous authentication gate verified"
            return result
        identity = client.get(base_url + "/api/v1/auth/me", auth=(settings.public_access_username, settings.public_access_password.get_secret_value()))
        result["authenticated_status"] = identity.status_code
        result["ready"] = identity.status_code == 200 and identity.json().get("role") == "system_admin" and identity.json().get("user_id") == settings.public_access_user_id
        if not result["ready"]:
            result["error"] = "ADMIN_IDENTITY_CHECK_FAILED"
    except (httpx.HTTPError, ValueError, AttributeError, TypeError):
        result["error"] = "CONNECTION_OR_RESPONSE_FAILED"
    return result


def bootstrap_credentials_unchanged(settings: Settings) -> bool:
    from tunnel_qa.auth.service import check_password
    if not settings.database_url.startswith("sqlite:///") or settings.database_url.endswith(":memory:"):
        return True
    path = Path(settings.database_url.removeprefix("sqlite:///")).resolve()
    if not path.is_file():
        return True
    try:
        connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        try:
            if not connection.execute("SELECT 1 FROM sqlite_master WHERE name='user_accounts'").fetchone():
                return True
            account = connection.execute("SELECT username,password_hash,enabled FROM user_accounts WHERE id=?", (settings.public_access_user_id,)).fetchone()
            return bool(account and account[2] and account[0] == settings.public_access_username.lower() and settings.public_access_password and check_password(settings.public_access_password.get_secret_value(), account[1]))
        finally:
            connection.close()
    except sqlite3.Error:
        return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url")
    args = parser.parse_args()
    try:
        settings = Settings()
    except Exception:
        print(json.dumps({"ready": False, "error": "INVALID_CONFIGURATION"}))
        return 1
    if not args.base_url:
        result = {"ready": configuration_ready(settings)}
    else:
        with httpx.Client(timeout=8, follow_redirects=False, headers={"ngrok-skip-browser-warning": "true"}) as client:
            result = probe(args.base_url, settings, client, verify_admin=bootstrap_credentials_unchanged(settings))
    print(json.dumps(result))
    return 0 if result["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
