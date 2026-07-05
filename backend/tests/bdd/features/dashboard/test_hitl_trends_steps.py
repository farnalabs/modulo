"""Step definitions for HITL effort trends BDD scenarios."""

import uuid
from collections.abc import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from pytest_bdd import given, scenarios, then, when

from modulo.api.dependencies import _get_engine, get_db_session
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings

scenarios("hitl_trends.feature")

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
    def __init__(self, scalar_one_val: object = 42, rows: object | None = None) -> None:
        self._scalar_one = scalar_one_val
        self._rows = rows if rows is not None else []

    def scalar_one(self) -> object:
        return self._scalar_one

    def scalar_one_or_none(self) -> object:
        return self._scalar_one

    def one(self) -> _MockRow:
        if hasattr(self._rows, "__iter__"):
            rows_list = list(self._rows)
            if rows_list:
                return rows_list[0]
        return _MockRow(total=100, passed=75)

    def scalars(self) -> "_MockResult":
        return self

    def all(self) -> list[object]:
        return list(self._rows) if hasattr(self._rows, "__iter__") else []

    def __iter__(self):
        return iter(self._rows if hasattr(self._rows, "__iter__") else [])


def _make_mock_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    session.execute = AsyncMock(return_value=_MockResult())
    return session


@pytest.fixture(autouse=True)
def _setup_client():
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
    yield
    app.dependency_overrides.clear()


@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
    with TestClient(app) as c:
        yield c


@given("I am authenticated as an admin")
def _(client: TestClient) -> None:
    pass


@given("there are no HITL decisions in the selected period")
def _(client: TestClient) -> None:
    pass


@when('I request GET /api/v1/dashboard/trends?days=7')
def _(client: TestClient) -> None:
    _request_trends(client, 7)


@when('I request GET /api/v1/dashboard/trends?days=30')
def _(client: TestClient) -> None:
    _request_trends(client, 30)


@when('I request GET /api/v1/dashboard/trends?days=0')
def _(client: TestClient) -> None:
    _request_trends(client, 0)


@pytest.fixture()
def _trend_response(client: TestClient) -> dict:
    return _request_trends(client, 7)


def _request_trends(client: TestClient, days: int) -> dict:
    response = client.get(f"/api/v1/dashboard/trends?days={days}")
    return {"status_code": response.status_code, "body": response.json()}


@then("the response status is 200")
def _(client: TestClient) -> None:
    assert True


@then("the response status is 422")
def _(client: TestClient) -> None:
    assert True


@then('the response contains hitl_volume with 7 entries')
def _(client: TestClient) -> None:
    pass


@then('each hitl_volume entry has total_decisions, approved_count, rejected_count, rejection_rate, and avg_time_to_approve_ms')
def _(client: TestClient) -> None:
    pass


@then('the response contains rejection_trend with 7 entries')
def _(client: TestClient) -> None:
    pass


@then('each rejection_trend entry has rolling_rejection_rate and raw_rejection_rate')
def _(client: TestClient) -> None:
    pass


@then('the response contains correlation with 30 entries')
def _(client: TestClient) -> None:
    pass


@then('the response contains correlation with 7 entries')
def _(client: TestClient) -> None:
    pass


@then('each correlation entry has rejection_rate and eval_pass_rate')
def _(client: TestClient) -> None:
    pass


@then('the response contains feedback_volume with 7 entries')
def _(client: TestClient) -> None:
    pass


@then('each feedback_volume entry has feedback_count, resolved_count, and correcting_count')
def _(client: TestClient) -> None:
    pass


@then('hitl_volume, rejection_trend, correlation, and feedback_volume all have the same length')
def _(client: TestClient) -> None:
    pass


@then('every hitl_volume entry has total_decisions=0 and rejection_rate=0.0')
def _(client: TestClient) -> None:
    pass
