"""Unit tests for GET /api/v1/admin/runs/retention.

Regression coverage for the admin run-retention config endpoint against a
fresh/empty database: a NULL or non-numeric ``retention_days`` value inside the
org's ``settings_json`` used to raise a Pydantic ``ValidationError`` (HTTP 500)
because ``RetentionConfigResponse(retention_days=None)`` is invalid. The
handler must return 200 with the default (90) for empty/NULL/unusable values
and still return the stored value when one exists.
"""

import uuid
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


def _make_mock_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


class _EnterprisePlan:
    """Stub plan context that enables all features for tests."""

    def feature_enabled(self, name: str) -> bool:
        return True

    def list_enabled_features(self) -> list:
        return []


def _make_client(mock_session: AsyncMock) -> TestClient:
    """Build a TestClient whose db-session override yields *mock_session*."""
    app.dependency_overrides.clear()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_plan_context] = lambda: _EnterprisePlan()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="admin",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )
    return TestClient(app)


def _session_returning(settings_json: object) -> AsyncMock:
    mock_session = _make_mock_session()
    result = MagicMock()
    result.scalar_one_or_none.return_value = settings_json
    mock_session.execute.return_value = result
    return mock_session


@pytest.fixture()
def client() -> TestClient:
    yield TestClient(app)
    app.dependency_overrides.clear()


class TestAdminGetRetention:
    """GET /api/v1/admin/runs/retention against a fresh/empty database."""

    ENDPOINT = "/api/v1/admin/runs/retention"

    def _get(self, settings_json: object) -> object:
        mock_session = _session_returning(settings_json)
        test_client = _make_client(mock_session)
        try:
            with patch("modulo.api.routes.admin.set_rls_org", new=AsyncMock()):
                return test_client.get(self.ENDPOINT)
        finally:
            app.dependency_overrides.clear()

    def test_empty_settings_json_returns_default(self) -> None:
        """Empty settings_json (fresh DB default) returns 200 with default 90."""
        resp = self._get({})
        assert resp.status_code == 200
        assert resp.json() == {"retention_days": 90}

    def test_null_retention_days_returns_default(self) -> None:
        """A NULL retention_days value must not raise a 500 — return default 90."""
        resp = self._get({"retention_days": None})
        assert resp.status_code == 200
        assert resp.json() == {"retention_days": 90}

    def test_non_numeric_retention_days_returns_default(self) -> None:
        """Non-numeric junk in settings_json must not raise a 500."""
        resp = self._get({"retention_days": "abc"})
        assert resp.status_code == 200
        assert resp.json() == {"retention_days": 90}

    def test_boolean_retention_days_returns_default(self) -> None:
        """A boolean retention_days value must not be treated as a day count."""
        resp = self._get({"retention_days": True})
        assert resp.status_code == 200
        assert resp.json() == {"retention_days": 90}

    def test_stored_value_is_returned(self) -> None:
        """A populated settings_json returns the stored retention_days."""
        resp = self._get({"retention_days": 45})
        assert resp.status_code == 200
        assert resp.json() == {"retention_days": 45}

    def test_missing_org_row_returns_default(self) -> None:
        """A missing organisation row (scalar returns None) still returns 200."""
        resp = self._get(None)
        assert resp.status_code == 200
        assert resp.json() == {"retention_days": 90}
