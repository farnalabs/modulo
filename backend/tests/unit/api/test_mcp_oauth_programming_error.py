"""Unit tests for /api/v1/mcp/oauth/* — ProgrammingError→501, SQLAlchemyError→503."""

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
_PROG_ERROR = ProgrammingError("", {}, None)
_SQLA_ERROR = SQLAlchemyError("db error")


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
        modulo_public_url="https://modulo.example.com",
    )


def _make_mock_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


def _make_admin_principal() -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        username="admin",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )


def _make_runner_principal() -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        username="runner",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="runner",
    )


@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = _make_admin_principal
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture()
def runner_client() -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = _make_runner_principal
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


_VALID_PAYLOAD = {
    "name": "My App",
    "redirect_uris": ["https://app.example.com/callback"],
    "scopes": ["trigger:run"],
}


class TestRegisterOAuthClientProgrammingError:
    ENDPOINT = "/api/v1/mcp/oauth/clients"

    def test_returns_501_on_programming_error(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.mcp_oauth.create_oauth_client", side_effect=_PROG_ERROR),
            patch("modulo.api.routes.mcp_oauth.set_rls_org"),
        ):
            resp = client.post(self.ENDPOINT, json=_VALID_PAYLOAD)
        assert resp.status_code == 501
        assert "migrations" in resp.json()["detail"].lower()

    def test_returns_503_on_sqlalchemy_error(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.mcp_oauth.create_oauth_client", side_effect=_SQLA_ERROR),
            patch("modulo.api.routes.mcp_oauth.set_rls_org"),
        ):
            resp = client.post(self.ENDPOINT, json=_VALID_PAYLOAD)
        assert resp.status_code == 503

    def test_runner_gets_403_before_db_call(self, runner_client: TestClient) -> None:
        resp = runner_client.post(self.ENDPOINT, json=_VALID_PAYLOAD)
        assert resp.status_code == 403


class TestListOAuthClientsProgrammingError:
    ENDPOINT = "/api/v1/mcp/oauth/clients"

    def test_returns_501_on_programming_error(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.mcp_oauth.list_oauth_clients", side_effect=_PROG_ERROR),
            patch("modulo.api.routes.mcp_oauth.set_rls_org"),
        ):
            resp = client.get(self.ENDPOINT)
        assert resp.status_code == 501
        assert "migrations" in resp.json()["detail"].lower()

    def test_returns_503_on_sqlalchemy_error(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.mcp_oauth.list_oauth_clients", side_effect=_SQLA_ERROR),
            patch("modulo.api.routes.mcp_oauth.set_rls_org"),
        ):
            resp = client.get(self.ENDPOINT)
        assert resp.status_code == 503


class TestRemoveOAuthClientProgrammingError:
    ENDPOINT = "/api/v1/mcp/oauth/clients"

    def test_returns_501_on_programming_error(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.mcp_oauth.delete_oauth_client", side_effect=_PROG_ERROR),
            patch("modulo.api.routes.mcp_oauth.set_rls_org"),
        ):
            resp = client.delete(f"{self.ENDPOINT}/myclient123")
        assert resp.status_code == 501
        assert "migrations" in resp.json()["detail"].lower()

    def test_returns_503_on_sqlalchemy_error(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.mcp_oauth.delete_oauth_client", side_effect=_SQLA_ERROR),
            patch("modulo.api.routes.mcp_oauth.set_rls_org"),
        ):
            resp = client.delete(f"{self.ENDPOINT}/myclient123")
        assert resp.status_code == 503

    def test_runner_gets_403_before_db_call(self, runner_client: TestClient) -> None:
        resp = runner_client.delete(f"{self.ENDPOINT}/myclient123")
        assert resp.status_code == 403
