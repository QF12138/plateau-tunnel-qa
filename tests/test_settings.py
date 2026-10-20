from fastapi.testclient import TestClient

from tunnel_qa.app import create_app
from tunnel_qa.qa.model_gateway import ExtractiveFallbackGateway
from tunnel_qa.settings.models import SystemSetting
from tunnel_qa.core.secret_protection import PREFIX, protect_secret, reveal_secret
from tunnel_qa.core.errors import AppError
import pytest


def headers(role="system_admin"):
    return {
        "X-User-Id": "settings-admin",
        "X-Role": role,
        "X-Security-Level": "confidential",
    }


def payload(**updates):
    data = {
        "model_mode": "local",
        "api_provider": "deepseek",
        "api_base_url": "https://api.deepseek.com",
        "api_model": "deepseek-chat",
        "local_provider": "builtin",
        "local_base_url": "http://127.0.0.1:11434",
        "local_model": "qwen2.5:7b",
        "llm_temperature": 0.1,
        "llm_timeout_seconds": 60,
        "retrieval_top_k": 8,
        "minimum_evidence_score": 0.12,
        "default_response_mode": "detailed",
        "auto_report": False,
        "report_format": "markdown",
        "theme": "light",
        "density": "comfortable",
        "audit_enabled": True,
        "safety_review_enabled": True,
    }
    data.update(updates)
    return data


def test_settings_default_response_applies_and_safety_cannot_be_disabled(database, settings):
    client = TestClient(create_app(settings, database))
    response = client.put("/api/v1/settings", headers=headers(), json=payload(default_response_mode="brief"))
    assert response.status_code == 200
    assert settings.default_response_mode == "brief"
    for option in ("audit_enabled", "safety_review_enabled"):
        rejected = client.put("/api/v1/settings", headers=headers(), json=payload(**{option: False}))
        assert rejected.status_code == 422
    state = client.get("/api/v1/settings", headers=headers()).json()
    assert state["audit_enabled"] and state["safety_review_enabled"]


def test_settings_require_system_administrator(database, settings):
    client = TestClient(create_app(settings, database))

    response = client.get("/api/v1/settings", headers=headers("field_engineer"))

    assert response.status_code == 403
    assert response.json()["error_code"] == "PERMISSION_DENIED"


def test_local_model_settings_switch_runtime_gateway_and_persist(database, settings):
    app = create_app(settings, database)
    client = TestClient(app)

    response = client.put(
        "/api/v1/settings",
        headers=headers(),
        json=payload(
            retrieval_top_k=12,
            minimum_evidence_score=0.2,
            default_response_mode="brief",
            auto_report=True,
            report_format="html",
            theme="dark",
            density="compact",
        ),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["model_mode"] == "local"
    assert body["active_provider"] == "local_fallback"
    assert body["retrieval_top_k"] == 12
    assert body["auto_report"] is True
    assert isinstance(app.state.model_gateway, ExtractiveFallbackGateway)

    restarted = create_app(settings, database)
    restarted_client = TestClient(restarted)
    persisted = restarted_client.get("/api/v1/settings", headers=headers()).json()
    assert persisted["theme"] == "dark"
    assert persisted["density"] == "compact"
    assert persisted["report_format"] == "html"


def test_api_settings_mask_secret_and_activate_deepseek(database, settings):
    app = create_app(settings, database)
    client = TestClient(app)
    secret = "sk-test-settings-secret-9012"

    response = client.put(
        "/api/v1/settings",
        headers=headers(),
        json=payload(
            model_mode="api",
            api_key=secret,
            llm_temperature=0.25,
        ),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["active_provider"] == "deepseek"
    assert body["active_model"] == "deepseek-chat"
    assert body["api_key_configured"] is True
    assert body["api_key_masked"] == "••••••••9012"
    assert secret not in response.text
    assert app.state.model_gateway.api_key == secret
    assert app.state.model_gateway.temperature == 0.25
    with database.session_factory() as session:
        saved = session.get(SystemSetting, "system").api_key_secret
        assert saved.startswith(PREFIX)
        assert secret not in saved
    restarted = create_app(settings, database)
    assert restarted.state.model_gateway.api_key == secret


def test_legacy_saved_key_is_migrated_without_plaintext_response(database, settings):
    secret = "sk-legacy-test-1111"
    with database.session_factory() as session:
        session.add(SystemSetting(id="system", model_mode="api", api_key_secret=secret))
        session.commit()
    app = create_app(settings, database)
    with database.session_factory() as session:
        assert session.get(SystemSetting, "system").api_key_secret.startswith(PREFIX)
    assert app.state.model_gateway.api_key == secret


def test_saved_ciphertext_cannot_be_tampered_with(settings):
    protected = protect_secret("synthetic-test-key", settings)
    with pytest.raises(AppError, match="无法解密"):
        reveal_secret(protected[:-5] + "xxxxx", settings)


def test_clearing_saved_key_also_clears_runtime_key(database, settings):
    app = create_app(settings, database)
    client = TestClient(app)
    client.put("/api/v1/settings", headers=headers(), json=payload(model_mode="api", api_key="synthetic-key"))
    response = client.put("/api/v1/settings", headers=headers(), json=payload(clear_api_key=True))
    assert response.json()["api_key_configured"] is False
    assert settings.deepseek_api_key is None


def test_windows_system_credential_roundtrip(settings):
    import sys
    if sys.platform != "win32":
        pytest.skip("Windows DPAPI verification")
    settings.secrets_encryption_key = None
    value = protect_secret("synthetic-windows-secret", settings)
    assert value.startswith(PREFIX + "dpapi:")
    assert reveal_secret(value, settings) == "synthetic-windows-secret"


def test_api_mode_rejects_missing_key(database, settings):
    client = TestClient(create_app(settings, database))

    response = client.put(
        "/api/v1/settings",
        headers=headers(),
        json=payload(model_mode="api", clear_api_key=True),
    )

    assert response.status_code == 400
    assert response.json()["error_code"] == "MODEL_API_KEY_REQUIRED"


def test_settings_validate_service_url(database, settings):
    client = TestClient(create_app(settings, database))

    response = client.put(
        "/api/v1/settings",
        headers=headers(),
        json=payload(local_base_url="file:///tmp/model"),
    )

    assert response.status_code == 422
    assert response.json()["error_code"] == "VALIDATION_ERROR"
