import json

import httpx
import pytest

from scripts.public_service_probe import configuration_ready, probe
from tunnel_qa.core.config import Settings


@pytest.fixture
def launch_settings():
    return Settings(_env_file=None, public_access_enabled=True,
                    public_access_password="synthetic-launch-password")


def transport(*, anonymous=401, role="system_admin", health="ok", redirect=False):
    def handle(request):
        if redirect:
            return httpx.Response(302, headers={"location": "https://other.invalid"})
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"status": health})
        if "authorization" in request.headers:
            return httpx.Response(200, json={"user_id": "web-demo", "role": role})
        return httpx.Response(anonymous, headers={"www-authenticate": 'Basic realm="test"'})
    return httpx.MockTransport(handle)


def test_probe_checks_anonymous_and_authenticated_access(launch_settings):
    with httpx.Client(transport=transport()) as client:
        result = probe("https://example.invalid", launch_settings, client)
    assert result["ready"]
    assert result["anonymous_status"] == 401
    assert "synthetic-launch-password" not in json.dumps(result)


@pytest.mark.parametrize("options", [
    {"anonymous": 200}, {"anonymous": 429}, {"role": "field_engineer"},
    {"health": "error"}, {"redirect": True},
])
def test_probe_refuses_unprotected_or_wrong_service(launch_settings, options):
    with httpx.Client(transport=transport(**options)) as client:
        assert not probe("https://example.invalid", launch_settings, client)["ready"]


@pytest.mark.parametrize("url", ["http://example.invalid", "https://user:pass@example.invalid", "https://example.invalid/path", "https://example.invalid?secret=x"])
def test_probe_rejects_unsafe_urls_before_request(launch_settings, url):
    def handle(request):
        pytest.fail("Unsafe URL must not receive credentials")
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        assert probe(url, launch_settings, client)["error"] == "UNSAFE_PROBE_URL"


def test_probe_masks_transport_errors(launch_settings):
    def handle(request):
        raise httpx.ConnectError("synthetic-launch-password", request=request)
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        result = probe("http://127.0.0.1:8765", launch_settings, client)
    assert not result["ready"]
    assert "synthetic-launch-password" not in json.dumps(result)


def test_configuration_fail_closed(launch_settings):
    assert configuration_ready(launch_settings)
    assert not configuration_ready(launch_settings.model_copy(update={"public_access_enabled": False}))
    assert not configuration_ready(launch_settings.model_copy(update={"public_access_password": None}))


def test_managed_password_probe_does_not_send_old_credentials(launch_settings):
    def handle(request):
        assert "authorization" not in request.headers
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/login":
            return httpx.Response(200, text='<form id="login-form"></form>')
        return httpx.Response(401, headers={"www-authenticate": 'Basic realm="test"'})
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        result = probe("https://example.invalid", launch_settings, client, verify_admin=False)
    assert result["ready"] and not result["admin_login_verified"]
