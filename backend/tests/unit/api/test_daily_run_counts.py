"""Unit tests for GET /api/v1/dashboard/daily-run-counts."""

import uuid
from collections.abc import AsyncGenerator, Generator, Sequence
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

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


class _MockRow:
    def __init__(self, **kwargs: object) -> None:
        for k, v in kwargs.items():
            setattr(self, k, v)


class _MockResult:
    def __init__(self, rows: Sequence[object] | None = None) -> None:
        self._rows = rows if rows is not None else []

    def scalar_one(self) -> object:
        return 42

    def scalars(self) -> "_MockResult":
        return self

    def all(self) -> list[object]:
        return list(self._rows)

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(self._rows)


def _make_mock_session(rows: Sequence[object] | None = None) -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)

    def _execute_side_effect(*_args: object, **_kwargs: object) -> _MockResult:
        return _MockResult(rows=rows)

    session.execute = AsyncMock(side_effect=_execute_side_effect)
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
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture()
def client_with_data() -> Generator[TestClient, None, None]:
    today = datetime.now(UTC)
    rows = [
        _MockRow(day=today - timedelta(days=2), status="complete", count=5),
        _MockRow(day=today - timedelta(days=2), status="failed", count=1),
        _MockRow(day=today - timedelta(days=1), status="running", count=3),
        _MockRow(day=today - timedelta(days=1), status="complete", count=7),
        _MockRow(day=today, status="pending", count=2),
        _MockRow(day=today, status="complete", count=4),
    ]
    mock_session = _make_mock_session(rows=rows)

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
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture()
def unauth_client() -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_settings] = _make_settings
    yield TestClient(app)
    app.dependency_overrides.clear()


class TestDailyRunCounts:
    """GET /api/v1/dashboard/daily-run-counts"""

    def test_returns_daily_counts(self, client: TestClient) -> None:
        response = client.get("/api/v1/dashboard/daily-run-counts")
        assert response.status_code == 200
        body = response.json()
        assert "daily_counts" in body
        assert "days" in body
        assert isinstance(body["daily_counts"], dict)
        assert isinstance(body["days"], int)

    def test_default_days(self, client: TestClient) -> None:
        response = client.get("/api/v1/dashboard/daily-run-counts")
        assert response.status_code == 200
        assert response.json()["days"] == 30

    def test_custom_days(self, client: TestClient) -> None:
        response = client.get("/api/v1/dashboard/daily-run-counts?days=7")
        assert response.status_code == 200
        assert response.json()["days"] == 7

    def test_accepts_max_days(self, client: TestClient) -> None:
        response = client.get("/api/v1/dashboard/daily-run-counts?days=365")
        assert response.status_code == 200

    def test_rejects_zero_days(self, client: TestClient) -> None:
        response = client.get("/api/v1/dashboard/daily-run-counts?days=0")
        assert response.status_code == 422

    def test_rejects_negative_days(self, client: TestClient) -> None:
        response = client.get("/api/v1/dashboard/daily-run-counts?days=-1")
        assert response.status_code == 422

    def test_rejects_excessive_days(self, client: TestClient) -> None:
        response = client.get("/api/v1/dashboard/daily-run-counts?days=366")
        assert response.status_code == 422

    def test_requires_auth(self, unauth_client: TestClient) -> None:
        response = unauth_client.get("/api/v1/dashboard/daily-run-counts")
        assert response.status_code in (401, 403)

    def test_groups_counts_by_status(self, client_with_data: TestClient) -> None:
        response = client_with_data.get("/api/v1/dashboard/daily-run-counts?days=7")
        assert response.status_code == 200
        body = response.json()
        daily = body["daily_counts"]

        assert len(daily) <= 7
        for day_key, status_map in daily.items():
            assert isinstance(day_key, str)
            for status, count in status_map.items():
                assert isinstance(status, str)
                assert isinstance(count, int)
                assert count > 0

    def test_returns_empty_dict_when_no_runs(self, client: TestClient) -> None:
        response = client.get("/api/v1/dashboard/daily-run-counts")
        assert response.status_code == 200
        body = response.json()
        assert body["daily_counts"] == {}
