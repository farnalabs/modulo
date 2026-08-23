"""Unit tests for the admin dev-mode API endpoint."""

from collections.abc import Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    app.dependency_overrides[get_db_session] = lambda: MagicMock()
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="testuser",
        organisation_id="00000000-0000-0000-0000-000000000001",
        account_id="00000000-0000-0000-0000-000000000002",
        org_role="admin",
        is_system_admin=True,
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def unauth_client() -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_settings] = _make_settings
    yield TestClient(app)
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# GET /api/v1/admin/dev-mode
# ---------------------------------------------------------------------------


class TestGetDevMode:
    def test_default_returns_false(self, client: TestClient) -> None:
        with patch("modulo.api.routes.admin_dev_mode.get_config", return_value=None):
            resp = client.get("/api/v1/admin/dev-mode")
        assert resp.status_code == 200
        body = resp.json()
        assert body["enabled"] is False
        assert body["source"] == "default"

    def test_env_var_returns_true(self, client: TestClient) -> None:
        settings = _make_settings()
        settings.modulo_dev_mode = True
        app.dependency_overrides[get_settings] = lambda: settings
        with patch("modulo.api.routes.admin_dev_mode.get_config", return_value=None):
            resp = client.get("/api/v1/admin/dev-mode")
        assert resp.status_code == 200
        body = resp.json()
        assert body["enabled"] is True
        assert body["source"] == "env"

    def test_db_override_returns_true(self, client: TestClient) -> None:
        config_mock = MagicMock()
        config_mock.value = True
        with patch("modulo.api.routes.admin_dev_mode.get_config", return_value=config_mock):
            resp = client.get("/api/v1/admin/dev-mode")
        assert resp.status_code == 200
        body = resp.json()
        assert body["enabled"] is True
        assert body["source"] == "db"

    def test_db_override_disabled(self, client: TestClient) -> None:
        config_mock = MagicMock()
        config_mock.value = False
        with patch("modulo.api.routes.admin_dev_mode.get_config", return_value=config_mock):
            resp = client.get("/api/v1/admin/dev-mode")
        assert resp.status_code == 200
        body = resp.json()
        assert body["enabled"] is False
        assert body["source"] == "db"

    def test_unauthenticated_returns_4xx(self, unauth_client: TestClient) -> None:
        resp = unauth_client.get("/api/v1/admin/dev-mode")
        assert resp.status_code in (401, 403)

    def test_db_error_falls_back(self, client: TestClient) -> None:
        settings = _make_settings()
        settings.modulo_dev_mode = True
        app.dependency_overrides[get_settings] = lambda: settings
        with patch("modulo.api.routes.admin_dev_mode.get_config", side_effect=RuntimeError("DB error")):
            resp = client.get("/api/v1/admin/dev-mode")
        assert resp.status_code == 200
        body = resp.json()
        assert body["enabled"] is True
        assert body["source"] == "env"


# ---------------------------------------------------------------------------
# PUT /api/v1/admin/dev-mode
# ---------------------------------------------------------------------------


class TestSetDevMode:
    def test_enable_returns_200(self, client: TestClient) -> None:
        with patch("modulo.api.routes.admin_dev_mode.update_config", new_callable=AsyncMock) as mock_set:
            resp = client.put("/api/v1/admin/dev-mode", json={"enabled": True})
        assert resp.status_code == 200
        body = resp.json()
        assert body["enabled"] is True
        assert body["source"] == "db"
        mock_set.assert_awaited_once()

    def test_disable_returns_200(self, client: TestClient) -> None:
        with patch("modulo.api.routes.admin_dev_mode.update_config", new_callable=AsyncMock) as mock_set:
            resp = client.put("/api/v1/admin/dev-mode", json={"enabled": False})
        assert resp.status_code == 200
        body = resp.json()
        assert body["enabled"] is False
        assert body["source"] == "db"
        mock_set.assert_awaited_once()

    def test_unauthenticated_returns_4xx(self, unauth_client: TestClient) -> None:
        resp = unauth_client.put("/api/v1/admin/dev-mode", json={"enabled": True})
        assert resp.status_code in (401, 403)

    def test_db_error_returns_500(self, client: TestClient) -> None:
        with patch("modulo.api.routes.admin_dev_mode.update_config", side_effect=RuntimeError("DB error")):
            resp = client.put("/api/v1/admin/dev-mode", json={"enabled": True})
        assert resp.status_code == 500
        body = resp.json()
        assert "detail" in body
