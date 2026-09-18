"""Step definitions for Daily Run Counts BDD scenarios (PRD §8.20).

Wires the scenarios in ``daily_run_counts.feature`` to the real
``GET /api/v1/dashboard/daily-run-counts`` route
(``modulo/api/routes/dashboard.py``) through the shared TestClient +
mock-session pattern. The mock session returns ``(day, status, count)`` rows
for the route's single grouping query, so the scenarios assert the actual
API contract — day/status keying, cross-status accumulation, the default 30
and custom ``days`` windows, and the 1..365 ``days`` bound.
"""

import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient
from pytest_bdd import given, parsers, scenarios, then, when

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_tenant_user
from modulo.auth.jwt import TenantPrincipal
from modulo.settings import Settings, get_settings

scenarios("daily_run_counts.feature")

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_ACCOUNT_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")


class _Row:
    """Simulates a SQLAlchemy result row with named attribute access."""

    def __init__(self, **kwargs: object) -> None:
        for k, v in kwargs.items():
            setattr(self, k, v)


class _Result:
    """Simulates a SQLAlchemy result proxy for chain/iteration calls."""

    def __init__(self, rows: list[_Row] | None = None, scalar_one_val: object = 0) -> None:
        self._rows = rows if rows is not None else []
        self._scalar_one = scalar_one_val

    def scalar_one(self) -> object:
        return self._scalar_one

    def scalar_one_or_none(self) -> object:
        return self._scalar_one

    def scalars(self) -> "_Result":
        return self

    def all(self) -> list[_Row]:
        return self._rows

    def __iter__(self):
        return iter(self._rows)


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key="a" * 32,
        fernet_key="a" * 32,
        modulo_admin_password="testpass",
        modulo_license_key="test-license-key",
        modulo_csrf_enabled=False,
    )


def _make_daily_session(rows: list[_Row] | None = None) -> AsyncMock:
    """AsyncSession double returning ``(day, status, count)`` grouping rows."""
    session = AsyncMock()
    session.info = {}
    bind = MagicMock()
    bind.dialect.name = "sqlite"
    session.get_bind = AsyncMock(return_value=bind)
    session.in_transaction = MagicMock(return_value=True)
    begin_cm = MagicMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)

    def _execute_side_effect(stmt: object, *_args: object, **_kwargs: object) -> _Result:
        text = str(stmt).lower()
        if "authz_enforce" in text:
            return _Result(scalar_one_val=None)
        return _Result(rows=rows or [])

    session.execute = AsyncMock(side_effect=_execute_side_effect)
    return session


def _make_admin_client(session: AsyncMock) -> Generator[TestClient, None, None]:
    """Build a TestClient with an admin tenant principal and mock session."""

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_tenant_user] = lambda: TenantPrincipal(
        username="admin",
        organisation_id=_ORG_ID,
        account_id=_ACCOUNT_ID,
        org_role="admin",
    )
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_settings, None)
        app.dependency_overrides.pop(get_db_session, None)
        app.dependency_overrides.pop(_get_engine, None)
        app.dependency_overrides.pop(get_current_tenant_user, None)
        app.dependency_overrides.pop(get_plan_context, None)


def _capture(url: str, request: Any, session: AsyncMock) -> None:
    client_gen = _make_admin_client(session)
    client = next(client_gen)
    try:
        resp = client.get(url)
    finally:
        client_gen.close()
    request.node._resp = resp


def _body(request: Any) -> dict[str, Any]:
    resp = request.node._resp
    body = resp.json()
    assert isinstance(body, dict), f"Expected a JSON object body, got {type(body)}"
    return body


def _today() -> str:
    return datetime.now(UTC).date().isoformat()


# ---------------------------------------------------------------------------
# Given
# ---------------------------------------------------------------------------


@given("I am authenticated as an admin")
def _given_auth_admin() -> None:
    """No-op — the ``when`` steps build an admin-principal TestClient."""


@given(parsers.re(r'runs exist today with (?P<spec>[^"]+)'))
def _given_runs_today(spec: str, request: Any) -> None:
    day = datetime.now(UTC).date()
    rows = [
        _Row(day=day, status=parts[1].strip(), cnt=int(parts[0].strip()))
        for clause in spec.split(" and ")
        if (parts := clause.strip().split()) and len(parts) == 2
    ]
    request.node._ctx = {"rows": rows}


def _rows(request: Any) -> list[_Row]:
    return cast("list[_Row]", getattr(request.node, "_ctx", {}).get("rows", []))


# ---------------------------------------------------------------------------
# When
# ---------------------------------------------------------------------------


@when(parsers.re(r"I request GET /api/v1/dashboard/daily-run-counts(?P<query>[^ ]*)"))
def _when_request_daily(request: Any, query: str = "") -> None:
    session = _make_daily_session(_rows(request) or None)
    _capture("/api/v1/dashboard/daily-run-counts" + query, request, session)


# ---------------------------------------------------------------------------
# Then
# ---------------------------------------------------------------------------


@then("the daily counts are keyed by day with per-status counts")
def _then_daily_keyed(request: Any) -> None:
    body = _body(request)
    assert "daily_counts" in body
    assert isinstance(body["daily_counts"], dict)


@then("today's counts include 4 complete and 2 failed")
def _then_today_counts(request: Any) -> None:
    today = _body(request)["daily_counts"].get(_today())
    assert today is not None, "no counts for today"
    assert today.get("complete") == 4
    assert today.get("failed") == 2


@then("today's counts total 6 runs")
def _then_today_total(request: Any) -> None:
    today = _body(request)["daily_counts"].get(_today())
    assert today is not None, "no counts for today"
    assert sum(today.values()) == 6


@then(parsers.parse("the response reports a days window of {days:d}"))
def _then_days_window(request: Any, days: int) -> None:
    assert _body(request)["days"] == days
