"""Unit tests for admin runtime-config route exception handling."""

from collections.abc import Generator
from unittest.mock import MagicMock, patch

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


@pytest.fixture()
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
    )
    _plan = MagicMock()
    _plan.feature_enabled = MagicMock(return_value=True)
    app.dependency_overrides[get_plan_context] = lambda: _plan
    yield TestClient(app)
    app.dependency_overrides.clear()


class TestGetRuntimeConfigErrorHandling:
    def test_exception_becomes_500(self, client: TestClient) -> None:
        with patch(
            "modulo.api.routes.admin_runtime_config.get_runtime_config_store",
            side_effect=RuntimeError("unexpected"),
        ):
            resp = client.get("/api/v1/admin/runtime-config")
        assert resp.status_code == 500
        body = resp.json()
        assert "Failed to list runtime configuration" in body["detail"]

    def test_value_error_becomes_500(self, client: TestClient) -> None:
        with patch(
            "modulo.api.routes.admin_runtime_config.get_runtime_config_store",
            side_effect=ValueError("bad value"),
        ):
            resp = client.get("/api/v1/admin/runtime-config")
        assert resp.status_code == 500
        body = resp.json()
        assert "Failed to list runtime configuration" in body["detail"]

    def test_type_error_becomes_500(self, client: TestClient) -> None:
        with patch(
            "modulo.api.routes.admin_runtime_config.get_runtime_config_store",
            side_effect=TypeError("wrong type"),
        ):
            resp = client.get("/api/v1/admin/runtime-config")
        assert resp.status_code == 500
        body = resp.json()
        assert "Failed to list runtime configuration" in body["detail"]


class TestPutRuntimeConfigErrorHandling:
    def test_exception_becomes_500(self, client: TestClient) -> None:
        with patch(
            "modulo.api.routes.admin_runtime_config.get_runtime_config_store",
            side_effect=RuntimeError("unexpected"),
        ):
            resp = client.put("/api/v1/admin/runtime-config", json={"overrides": {"MODULO_LOG_LEVEL": "DEBUG"}})
        assert resp.status_code == 500
        body = resp.json()
        assert "Failed to update runtime configuration" in body["detail"]

    def test_non_dict_overrides_returns_400(self, client: TestClient) -> None:
        resp = client.put("/api/v1/admin/runtime-config", json={"overrides": "not-a-dict"})
        assert resp.status_code == 400
        body = resp.json()
        assert "overrides' must be a dict" in body["detail"]

    def test_non_string_override_value_returns_400(self, client: TestClient) -> None:
        resp = client.put("/api/v1/admin/runtime-config", json={"overrides": {"MODULO_LOG_LEVEL": 123}})
        assert resp.status_code == 400
        body = resp.json()
        assert "must be a string" in body["detail"]

    def test_unknown_key_returns_400(self, client: TestClient) -> None:
        resp = client.put("/api/v1/admin/runtime-config", json={"overrides": {"UNKNOWN_KEY": "val"}})
        assert resp.status_code == 400
        body = resp.json()
        assert "Unknown config key" in body["detail"]

    def test_non_list_clear_returns_400(self, client: TestClient) -> None:
        resp = client.put("/api/v1/admin/runtime-config", json={"clear": "not-a-list"})
        assert resp.status_code == 400
        body = resp.json()
        assert "clear' must be a list" in body["detail"]

    def test_clear_non_string_key_returns_400(self, client: TestClient) -> None:
        resp = client.put("/api/v1/admin/runtime-config", json={"clear": [123]})
        assert resp.status_code == 400
        body = resp.json()
        assert "Clear key must be a string" in body["detail"]

    def test_clear_unknown_key_returns_400(self, client: TestClient) -> None:
        resp = client.put("/api/v1/admin/runtime-config", json={"clear": ["UNKNOWN_KEY"]})
        assert resp.status_code == 400
        body = resp.json()
        assert "Unknown config key" in body["detail"]


class TestPostReloadErrorHandling:
    def test_exception_becomes_500(self, client: TestClient) -> None:
        with patch(
            "modulo.api.routes.admin_runtime_config.get_runtime_config_store",
            side_effect=RuntimeError("unexpected"),
        ):
            resp = client.post("/api/v1/admin/runtime-config/reload")
        assert resp.status_code == 500
        body = resp.json()
        assert "Failed to reload runtime configuration" in body["detail"]

    def test_value_error_becomes_500(self, client: TestClient) -> None:
        with patch(
            "modulo.api.routes.admin_runtime_config.get_runtime_config_store",
            side_effect=ValueError("bad value"),
        ):
            resp = client.post("/api/v1/admin/runtime-config/reload")
        assert resp.status_code == 500
        body = resp.json()
        assert "Failed to reload runtime configuration" in body["detail"]
