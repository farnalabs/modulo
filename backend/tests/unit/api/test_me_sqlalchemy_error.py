"""Tests for SQLAlchemyError→503 handling in /api/v1/me routes."""

import uuid
from collections.abc import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError

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


@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="testuser",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


class TestChangePasswordSQLAlchemyError:
    _PASSWORD_PAYLOAD = {
        "current_password": "correct-horse-battery",
        "new_password": "new-strong-password-42",
    }
    _SETTINGS_PAYLOAD = {"theme": "dark"}

    def test_change_password_programming_error_returns_501(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.me.get_account_by_id", side_effect=ProgrammingError("stmt", {}, None)),
            patch("modulo.api.routes.me.list_families_for_account", return_value=[]),
            patch("modulo.api.routes.me.blacklist_family", return_value=True),
        ):
            resp = client.put("/api/v1/me/password", json=self._PASSWORD_PAYLOAD)
        assert resp.status_code == 501
        assert "database migrations" in resp.json()["detail"].lower()

    def test_change_password_sqlalchemy_error_returns_503(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.me.get_account_by_id", side_effect=SQLAlchemyError("connection failed")),
            patch("modulo.api.routes.me.list_families_for_account", return_value=[]),
            patch("modulo.api.routes.me.blacklist_family", return_value=True),
        ):
            resp = client.put("/api/v1/me/password", json=self._PASSWORD_PAYLOAD)
        assert resp.status_code == 503
        assert "database error" in resp.json()["detail"].lower()

    def test_get_settings_programming_error_returns_501(self, client: TestClient) -> None:
        with patch("modulo.api.routes.me.get_account_by_id", side_effect=ProgrammingError("stmt", {}, None)):
            resp = client.get("/api/v1/me/settings")
        assert resp.status_code == 501

    def test_get_settings_sqlalchemy_error_returns_503(self, client: TestClient) -> None:
        with patch("modulo.api.routes.me.get_account_by_id", side_effect=SQLAlchemyError("connection failed")):
            resp = client.get("/api/v1/me/settings")
        assert resp.status_code == 503

    def test_update_settings_programming_error_returns_501(self, client: TestClient) -> None:
        with patch("modulo.api.routes.me.update_account_preferences", side_effect=ProgrammingError("stmt", {}, None)):
            resp = client.put("/api/v1/me/settings", json=self._SETTINGS_PAYLOAD)
        assert resp.status_code == 501

    def test_update_settings_sqlalchemy_error_returns_503(self, client: TestClient) -> None:
        with patch("modulo.api.routes.me.update_account_preferences", side_effect=SQLAlchemyError("connection failed")):
            resp = client.put("/api/v1/me/settings", json=self._SETTINGS_PAYLOAD)
        assert resp.status_code == 503
