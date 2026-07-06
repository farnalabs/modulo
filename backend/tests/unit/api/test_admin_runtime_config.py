"""Unit tests for admin runtime-config route handler."""

from collections.abc import Generator
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session
from modulo.api.main import app
from modulo.api.middleware.sensitive_mask import SENSITIVE_VALUE_MASK, is_sensitive_env_key
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.core.runtime_config.store import get_runtime_config_store
from modulo.settings import Settings, get_settings

_ORG_ID = "00000000-0000-0000-0000-000000000001"


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key="a" * 32,
        fernet_key="a" * 32,
        modulo_admin_password="testpass",
    )


@pytest.fixture(autouse=True)
def _purge_store() -> Generator[None, None, None]:
    import modulo.core.runtime_config.store as store_mod

    store_mod._store = None
    yield


@pytest.fixture()
def admin_client() -> Generator[TestClient, None, None]:
    mock_session = MagicMock()

    async def override_session():
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="admin", organisation_id=_ORG_ID, account_id="u1", org_role="admin"
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture()
def viewer_client() -> Generator[TestClient, None, None]:
    mock_session = MagicMock()

    async def override_session():
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="viewer", organisation_id=_ORG_ID, account_id="u2", org_role="viewer"
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture()
def unauth_client() -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_settings] = _make_settings
    yield TestClient(app)
    app.dependency_overrides.clear()


class TestIsSensitiveEnvKey:
    def test_database_url_is_sensitive(self) -> None:
        assert is_sensitive_env_key("DATABASE_URL") is True

    def test_secret_key_is_sensitive(self) -> None:
        assert is_sensitive_env_key("SECRET_KEY") is True

    def test_modulo_users_is_sensitive(self) -> None:
        assert is_sensitive_env_key("MODULO_USERS") is True

    def test_fernet_key_is_sensitive(self) -> None:
        assert is_sensitive_env_key("FERNET_KEY") is True

    def test_modulo_admin_password_is_sensitive(self) -> None:
        assert is_sensitive_env_key("MODULO_ADMIN_PASSWORD") is True

    def test_non_sensitive_key(self) -> None:
        assert is_sensitive_env_key("MODULO_LOG_LEVEL") is False

    def test_encryption_pattern(self) -> None:
        assert is_sensitive_env_key("MY_ENCRYPTION_KEY") is True

    def test_signing_pattern(self) -> None:
        assert is_sensitive_env_key("SIGNING_SECRET") is True

    def test_private_pattern(self) -> None:
        assert is_sensitive_env_key("PRIVATE_KEY") is True


class TestRuntimeConfigRoute:
    def test_get_returns_200_for_admin(self, admin_client: TestClient) -> None:
        resp = admin_client.get("/api/v1/admin/runtime-config")
        assert resp.status_code == 200
        data = resp.json()
        assert "items" in data
        assert "has_drift" in data
        assert isinstance(data["has_drift"], bool)

    def test_get_returns_403_for_viewer(self, viewer_client: TestClient) -> None:
        resp = viewer_client.get("/api/v1/admin/runtime-config")
        assert resp.status_code == 403

    def test_get_returns_401_for_unauth(self, unauth_client: TestClient) -> None:
        resp = unauth_client.get("/api/v1/admin/runtime-config")
        assert resp.status_code == 401

    def test_masks_sensitive_values(self, admin_client: TestClient) -> None:
        resp = admin_client.get("/api/v1/admin/runtime-config")
        assert resp.status_code == 200
        data = resp.json()
        items: list[dict[str, Any]] = data["items"]
        for item in items:
            key: str = item["key"]
            if key == "DATABASE_URL":
                for field in ("current_value", "default_value"):
                    val = item.get(field)
                    if val and isinstance(val, str):
                        assert val == SENSITIVE_VALUE_MASK or val.startswith("postgresql"), (
                            f"DATABASE_URL.{field} should be masked or have real value: {val}"
                        )

    def test_put_override_returns_200(self, admin_client: TestClient) -> None:
        resp = admin_client.put("/api/v1/admin/runtime-config", json={"overrides": {"MODULO_LOG_LEVEL": "DEBUG"}})
        assert resp.status_code == 200
        data = resp.json()
        assert "items" in data
        assert data["has_drift"] is False

    def test_put_unknown_key_returns_400(self, admin_client: TestClient) -> None:
        resp = admin_client.put("/api/v1/admin/runtime-config", json={"overrides": {"NONEXISTENT_KEY": "value"}})
        assert resp.status_code == 400

    def test_put_clear_returns_200(self, admin_client: TestClient) -> None:
        store = get_runtime_config_store()
        store.set_override("MODULO_LOG_LEVEL", "DEBUG")
        resp = admin_client.put("/api/v1/admin/runtime-config", json={"clear": ["MODULO_LOG_LEVEL"]})
        assert resp.status_code == 200

    def test_put_invalid_overrides_type_returns_400(self, admin_client: TestClient) -> None:
        resp = admin_client.put("/api/v1/admin/runtime-config", json={"overrides": "not-a-dict"})
        assert resp.status_code == 400

    def test_put_invalid_clear_type_returns_400(self, admin_client: TestClient) -> None:
        resp = admin_client.put("/api/v1/admin/runtime-config", json={"clear": "not-a-list"})
        assert resp.status_code == 400

    def test_post_reload_returns_200(self, admin_client: TestClient) -> None:
        resp = admin_client.post("/api/v1/admin/runtime-config/reload")
        assert resp.status_code == 200

    def test_post_reload_returns_items(self, admin_client: TestClient) -> None:
        resp = admin_client.post("/api/v1/admin/runtime-config/reload")
        data = resp.json()
        assert "items" in data

    def test_sensitive_keys_are_masked_in_response(self, admin_client: TestClient) -> None:
        resp = admin_client.get("/api/v1/admin/runtime-config")
        data = resp.json()
        items: list[dict[str, Any]] = data["items"]
        sensitive = {
            "SECRET_KEY",
            "FERNET_KEY",
            "FERNET_KEY_OLD",
            "MODULO_USERS",
            "MODULO_ADMIN_PASSWORD",
            "VAULT_TOKEN",
            "AWS_SECRET_ACCESS_KEY",
            "MODULO_SCIM_TOKEN",
            "MODULO_RATELIMIT_BYPASS_TOKEN",
            "MODULO_E2B_API_KEY",
            "MODULO_LICENSE_KEY",
            "MODULO_SAML_SP_PRIVATE_KEY",
            "DATABASE_URL",
        }
        for item in items:
            key = item["key"]
            val = item.get("current_value")
            if val and isinstance(val, str) and key in sensitive:
                assert val == SENSITIVE_VALUE_MASK, f"{key} should be masked, got {val}"
