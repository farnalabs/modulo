"""Tests for ProgrammingError → 501, SQLAlchemyError → 503, and Exception → 500
on all three dashboard endpoints: summary, trends, daily-run-counts."""

import uuid
from collections.abc import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError

from modulo.api.dependencies import _get_engine, get_db_session
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


def _make_failing_session(exc: Exception) -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    session.execute = AsyncMock(side_effect=exc)
    return session


def _setup_client(session_mock: AsyncMock) -> Generator[TestClient, None, None]:
    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield session_mock

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="testuser",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


_SUMMARY_URL = "/api/v1/dashboard/summary"
_TRENDS_URL = "/api/v1/dashboard/trends?days=7"
_DAILY_URL = "/api/v1/dashboard/daily-run-counts?days=7"


class TestDashboardSummaryErrorPaths:
    @pytest.fixture()
    def client(self) -> Generator[TestClient, None, None]:
        session = _make_failing_session(ProgrammingError("stmt", "params", "orig"))
        yield from _setup_client(session)

    def test_programming_error_returns_501(self, client: TestClient) -> None:
        response = client.get(_SUMMARY_URL)
        assert response.status_code == 501
        assert "migrations" in response.json()["detail"].lower()


class TestDashboardSummarySQLAlchemyError:
    @pytest.fixture()
    def client(self) -> Generator[TestClient, None, None]:
        session = _make_failing_session(SQLAlchemyError("connection failure"))
        yield from _setup_client(session)

    def test_sqlalchemy_error_returns_500(self, client: TestClient) -> None:
        response = client.get(_SUMMARY_URL)
        assert response.status_code == 500


class TestDashboardSummaryGenericError:
    @pytest.fixture()
    def client(self) -> Generator[TestClient, None, None]:
        session = _make_failing_session(RuntimeError("unexpected"))
        yield from _setup_client(session)

    def test_generic_exception_returns_500(self, client: TestClient) -> None:
        response = client.get(_SUMMARY_URL)
        assert response.status_code == 500


class TestDashboardTrendsErrorPaths:
    @pytest.fixture()
    def client(self) -> Generator[TestClient, None, None]:
        session = _make_failing_session(ProgrammingError("stmt", "params", "orig"))
        yield from _setup_client(session)

    def test_programming_error_returns_501(self, client: TestClient) -> None:
        response = client.get(_TRENDS_URL)
        assert response.status_code == 501
        assert "migrations" in response.json()["detail"].lower()


class TestDashboardTrendsSQLAlchemyError:
    @pytest.fixture()
    def client(self) -> Generator[TestClient, None, None]:
        session = _make_failing_session(SQLAlchemyError("connection failure"))
        yield from _setup_client(session)

    def test_sqlalchemy_error_returns_500(self, client: TestClient) -> None:
        response = client.get(_TRENDS_URL)
        assert response.status_code == 500


class TestDashboardTrendsGenericError:
    @pytest.fixture()
    def client(self) -> Generator[TestClient, None, None]:
        session = _make_failing_session(RuntimeError("unexpected"))
        yield from _setup_client(session)

    def test_generic_exception_returns_500(self, client: TestClient) -> None:
        response = client.get(_TRENDS_URL)
        assert response.status_code == 500


class TestDashboardDailyCountsErrorPaths:
    @pytest.fixture()
    def client(self) -> Generator[TestClient, None, None]:
        session = _make_failing_session(ProgrammingError("stmt", "params", "orig"))
        yield from _setup_client(session)

    def test_programming_error_returns_501(self, client: TestClient) -> None:
        response = client.get(_DAILY_URL)
        assert response.status_code == 501
        assert "migrations" in response.json()["detail"].lower()


class TestDashboardDailyCountsSQLAlchemyError:
    @pytest.fixture()
    def client(self) -> Generator[TestClient, None, None]:
        session = _make_failing_session(SQLAlchemyError("connection failure"))
        yield from _setup_client(session)

    def test_sqlalchemy_error_returns_500(self, client: TestClient) -> None:
        response = client.get(_DAILY_URL)
        assert response.status_code == 500


class TestDashboardDailyCountsGenericError:
    @pytest.fixture()
    def client(self) -> Generator[TestClient, None, None]:
        session = _make_failing_session(RuntimeError("unexpected"))
        yield from _setup_client(session)

    def test_generic_exception_returns_500(self, client: TestClient) -> None:
        response = client.get(_DAILY_URL)
        assert response.status_code == 500
