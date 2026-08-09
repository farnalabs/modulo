"""Unit tests for GET /api/v1/license (viewmodel.py license_info endpoint)."""

from collections.abc import Generator
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from modulo.api.dependencies import get_db_session, get_plan_context
from modulo.api.dependencies import get_settings as dep_get_settings
from modulo.api.main import app
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
    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = lambda: MagicMock()
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


class TestLicenseInfo:
    URL = "/api/v1/license"

    def test_no_key_returns_community(self, client: TestClient) -> None:
        resp = client.get(self.URL)
        assert resp.status_code == 200
        body = resp.json()
        assert body["tier"] == "community"
        assert body["features"] == []
        assert body["is_valid"] is True

    def test_with_key_returns_team(self, client: TestClient) -> None:
        settings_with_key = _make_settings()
        settings_with_key.modulo_license_key = "test-key"
        old_override = app.dependency_overrides.get(dep_get_settings)
        app.dependency_overrides[dep_get_settings] = lambda: settings_with_key
        try:
            resp = client.get(self.URL)
        finally:
            if old_override is not None:
                app.dependency_overrides[dep_get_settings] = old_override
            else:
                app.dependency_overrides.pop(dep_get_settings, None)
        assert resp.status_code == 200
        body = resp.json()
        assert body["tier"] == "team"
        assert "notifications" in body["features"]
        assert body["is_valid"] is True

    def test_is_valid_always_true(self, client: TestClient) -> None:
        resp = client.get(self.URL)
        assert resp.status_code == 200
        body = resp.json()
        assert body["is_valid"] is True

    def test_publicly_accessible(self, client: TestClient) -> None:
        """The /api/v1/license endpoint requires no auth."""
        resp = client.get(self.URL)
        assert resp.status_code == 200
