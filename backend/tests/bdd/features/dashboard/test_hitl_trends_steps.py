"""Step definitions for HITL effort trends BDD scenarios (PRD §8.8).

Wires the 7 scenarios in ``hitl_trends.feature`` to ``GET /api/v1/dashboard/trends``
through the shared TestClient + mock-session harness. The endpoint aggregates
HITL decision volume, rejection-rate trend, eval-pass-rate correlation, and
feedback volume; with an empty mock session it returns zero-filled arrays
aligned to the requested ``days`` range — which is exactly what the feature
scenarios assert on.
"""

import uuid
from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient
from pytest_bdd import given, parsers, scenarios, then, when
from tests.unit.api.mock_session import configure_mock_session

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_tenant_user
from modulo.auth.jwt import TenantPrincipal
from modulo.settings import Settings, get_settings

scenarios("hitl_trends.feature")

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_ACCOUNT_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key="a" * 32,
        fernet_key="a" * 32,
        modulo_admin_password="testpass",
        modulo_license_key="test-license-key",
        modulo_csrf_enabled=False,
    )


def _make_mock_session() -> AsyncMock:
    """AsyncSession double whose queries all return empty rows.

    ``allow_empty_execute=True`` stubs ``execute`` to an empty result, so the
    four trends queries (eval, daily run counts, HITL decisions, feedback) all
    yield no rows and the endpoint emits zero-filled series.
    """
    session = configure_mock_session(AsyncMock(), allow_empty_execute=True)
    begin_cm = MagicMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


def _make_admin_client(session: AsyncMock | None = None) -> TestClient:
    """Build a TestClient with an admin tenant principal and mock session."""
    session = session or _make_mock_session()

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
    return TestClient(app)


def _clean_overrides() -> None:
    app.dependency_overrides.pop(get_settings, None)
    app.dependency_overrides.pop(get_db_session, None)
    app.dependency_overrides.pop(_get_engine, None)
    app.dependency_overrides.pop(get_current_tenant_user, None)
    app.dependency_overrides.pop(get_plan_context, None)


def _request_trends(days: int, session: AsyncMock | None = None) -> Any:
    client = _make_admin_client(session)
    try:
        return client.get(f"/api/v1/dashboard/trends?days={days}")
    finally:
        _clean_overrides()


def _capture_trends(request, days: int) -> None:
    resp = _request_trends(days)
    request.node._resp = resp
    request.node.response = resp


@given("I am authenticated as an admin")
def _bdd_auth_admin() -> None:
    """No-op — the ``when`` steps build an admin-principal TestClient."""


@given("there are no HITL decisions in the selected period")
def _bdd_no_hitl_decisions() -> None:
    """No-op — the empty mock session yields zero decisions."""


@when("I request GET /api/v1/dashboard/trends?days=7")
def _bdd_request_trends_7(request) -> None:
    _capture_trends(request, 7)


@when("I request GET /api/v1/dashboard/trends?days=30")
def _bdd_request_trends_30(request) -> None:
    _capture_trends(request, 30)


@when("I request GET /api/v1/dashboard/trends?days=0")
def _bdd_request_trends_0(request) -> None:
    _capture_trends(request, 0)


def _assert_body(request) -> dict[str, Any]:
    resp = request.node._resp
    body = resp.json()
    assert isinstance(body, dict), f"Expected a JSON object body, got {type(body)}"
    return body


@then(parsers.parse("the response contains {key} with {count:d} entries"))
def _bdd_entries(request, key: str, count: int) -> None:
    body = _assert_body(request)
    assert key in body, f"Expected key '{key}' in response body, got {sorted(body)}"
    entries = body[key]
    assert isinstance(entries, list), f"Expected '{key}' to be a list, got {type(entries)}"
    assert len(entries) == count, f"Expected {count} '{key}' entries, got {len(entries)}"


@then(
    "each hitl_volume entry has total_decisions, approved_count,"
    " rejected_count, rejection_rate, and avg_time_to_approve_ms"
)
def _bdd_hitl_volume_shape(request) -> None:
    for entry in _assert_body(request)["hitl_volume"]:
        for field in (
            "date",
            "total_decisions",
            "approved_count",
            "rejected_count",
            "rejection_rate",
            "avg_time_to_approve_ms",
        ):
            assert field in entry, f"hitl_volume entry missing '{field}': {entry}"


@then("each rejection_trend entry has rolling_rejection_rate and raw_rejection_rate")
def _bdd_rejection_trend_shape(request) -> None:
    for entry in _assert_body(request)["rejection_trend"]:
        for field in ("date", "rolling_rejection_rate", "raw_rejection_rate"):
            assert field in entry, f"rejection_trend entry missing '{field}': {entry}"


@then("each correlation entry has rejection_rate and eval_pass_rate")
def _bdd_correlation_shape(request) -> None:
    for entry in _assert_body(request)["correlation"]:
        for field in ("date", "rejection_rate", "eval_pass_rate"):
            assert field in entry, f"correlation entry missing '{field}': {entry}"


@then("each feedback_volume entry has feedback_count, resolved_count, and correcting_count")
def _bdd_feedback_volume_shape(request) -> None:
    for entry in _assert_body(request)["feedback_volume"]:
        for field in ("date", "feedback_count", "resolved_count", "correcting_count"):
            assert field in entry, f"feedback_volume entry missing '{field}': {entry}"


@then("hitl_volume, rejection_trend, correlation, and feedback_volume all have the same length")
def _bdd_series_aligned(request) -> None:
    body = _assert_body(request)
    lengths = {key: len(body[key]) for key in ("hitl_volume", "rejection_trend", "correlation", "feedback_volume")}
    assert len(set(lengths.values())) == 1, f"Trend arrays are not aligned: {lengths}"


@then("every hitl_volume entry has total_decisions=0 and rejection_rate=0.0")
def _bdd_zero_filled(request) -> None:
    for entry in _assert_body(request)["hitl_volume"]:
        assert entry["total_decisions"] == 0, f"Expected zero decisions, got {entry}"
        assert entry["rejection_rate"] == 0.0, f"Expected zero rejection rate, got {entry}"
