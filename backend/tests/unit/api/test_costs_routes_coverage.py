"""Route-level coverage tests for the admin cost endpoints (FAR-618).

Complements ``test_costs.py`` (happy paths, gating, spend-limit semantics) by
covering the per-route DB error-convention matrices (ProgrammingError→501,
SQLAlchemyError→503, generic Exception→500), the 404 branches (missing org /
team / pipeline / report / anomaly), the cents-coercion fallbacks, the
cost-control update merge branches, the rolling-anomaly detection + merge
branches, and the degraded bucket aggregation on get_costs.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Generator
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError

from modulo.api.dependencies import get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_TEAM_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")
_PIPELINE_ID = uuid.uuid4()
_REPORT_ID = uuid.uuid4()
_ANOMALY_ID = uuid.uuid4()

_PROG = ProgrammingError("s", {}, Exception())
_SQL = SQLAlchemyError("boom")
_RUNTIME = RuntimeError("kaboom")

_PREFIX = "modulo.api.routes.costs."


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


def _make_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    execute_result = MagicMock()
    execute_result.scalar_one_or_none.return_value = None
    execute_result.all.return_value = []
    execute_result.scalars.return_value.all.return_value = []
    session.execute = AsyncMock(return_value=execute_result)
    return session


@contextmanager
def _rls_cm() -> Generator[None, None, None]:
    patches = [
        patch(f"{_PREFIX}set_rls_org", new_callable=AsyncMock),
        patch(f"{_PREFIX}set_rls_user_context", new_callable=AsyncMock),
    ]
    for p in patches:
        p.start()
    try:
        yield
    finally:
        for p in reversed(patches):
            p.stop()


@pytest.fixture
def client() -> Generator[tuple[TestClient, AsyncMock], None, None]:
    session = _make_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="admin@test", organisation_id=_ORG_ID, account_id=_USER_ID, org_role="admin"
    )
    plan = MagicMock()
    plan.feature_enabled.return_value = True
    plan.list_enabled_features.return_value = []
    app.dependency_overrides[get_plan_context] = lambda: plan
    yield TestClient(app), session
    app.dependency_overrides.clear()


def _assert_error_matrix(
    http: TestClient,
    *,
    method: str,
    url: str,
    json_body: dict | None,
    patch_target: str,
    expected: dict,
) -> None:
    for exc, status_code in expected:
        with ExitStack() as stack:
            stack.enter_context(patch(f"{_PREFIX}{patch_target}", new=AsyncMock(side_effect=exc)))
            stack.enter_context(_rls_cm())
            kwargs = {"json": json_body} if json_body is not None else {}
            resp = getattr(http, method.lower())(url, **kwargs)
        assert resp.status_code == status_code, f"{patch_target} {exc!r}: {resp.text}"


# ---------------------------------------------------------------------------
# Coercion helpers — invalid/absent values degrade to None
# ---------------------------------------------------------------------------


def test_coerce_spend_limit_usd_invalid_returns_none() -> None:
    from modulo.api.routes.costs import _coerce_spend_limit_usd

    assert _coerce_spend_limit_usd(None) is None
    assert _coerce_spend_limit_usd("not-a-number") is None  # type: ignore[arg-type]
    assert _coerce_spend_limit_usd(Decimal("12.5")) == 12.5


def test_coerce_cents_usd_invalid_returns_none() -> None:
    from modulo.api.routes.costs import _coerce_cents_usd

    assert _coerce_cents_usd(None) is None
    assert _coerce_cents_usd("bad") is None  # type: ignore[arg-type]
    assert _coerce_cents_usd(1500) == 15.0


# ---------------------------------------------------------------------------
# GET /admin/costs — error mapping + degraded bucket aggregation
# ---------------------------------------------------------------------------


def test_get_costs_assert_error_matrix(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    _assert_error_matrix(
        http,
        method="get",
        url="/api/v1/admin/costs",
        json_body=None,
        patch_target="get_cost_report",
        expected={(_PROG, 501), (_SQL, 503), (_RUNTIME, 500)},
    )


def test_get_costs_bucket_failure_degrades_to_empty(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    rows = [
        {
            "entity_id": str(_TEAM_ID),
            "entity_name": "Team A",
            "total_spend_usd": 1.5,
            "total_runs": 3,
        }
    ]
    with ExitStack() as stack:
        stack.enter_context(patch(f"{_PREFIX}get_cost_report", new=AsyncMock(return_value=rows)))
        stack.enter_context(patch(f"{_PREFIX}build_cost_report_buckets", new=AsyncMock(side_effect=ValueError("bad"))))
        stack.enter_context(_rls_cm())
        resp = http.get("/api/v1/admin/costs")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["items"][0]["total_spend_usd"] == 1.5
    assert not body["items"][0]["components"]


# ---------------------------------------------------------------------------
# GET/PUT /limits — error mapping + 404s
# ---------------------------------------------------------------------------


def test_get_spend_limits_assert_error_matrix(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    _assert_error_matrix(
        http,
        method="get",
        url="/api/v1/admin/costs/limits",
        json_body=None,
        patch_target="list_teams",
        expected={(_PROG, 501), (_SQL, 503), (_RUNTIME, 500)},
    )


def test_set_org_spend_limit_unknown_org_returns_404(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    with ExitStack() as stack:
        stack.enter_context(patch(f"{_PREFIX}get_organisation", new=AsyncMock(return_value=None)))
        stack.enter_context(_rls_cm())
        resp = http.put("/api/v1/admin/costs/limits/org", json={"daily_spend_limit": 5})

    assert resp.status_code == 404, resp.text


def test_set_org_spend_limit_assert_error_matrix(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    _assert_error_matrix(
        http,
        method="put",
        url="/api/v1/admin/costs/limits/org",
        json_body={"daily_spend_limit": 5},
        patch_target="get_organisation",
        expected={(_PROG, 501), (_SQL, 503), (_RUNTIME, 500)},
    )


def test_set_team_spend_limit_unknown_team_returns_404(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    with ExitStack() as stack:
        stack.enter_context(patch(f"{_PREFIX}get_team", new=AsyncMock(return_value=None)))
        stack.enter_context(_rls_cm())
        resp = http.put(f"/api/v1/admin/costs/limits/teams/{_TEAM_ID}", json={"daily_spend_limit": 5})

    assert resp.status_code == 404, resp.text


def test_set_team_spend_limit_cross_org_team_returns_404(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    team = MagicMock()
    team.organisation_id = uuid.uuid4()
    with ExitStack() as stack:
        stack.enter_context(patch(f"{_PREFIX}get_team", new=AsyncMock(return_value=team)))
        stack.enter_context(_rls_cm())
        resp = http.put(f"/api/v1/admin/costs/limits/teams/{_TEAM_ID}", json={"daily_spend_limit": 5})

    assert resp.status_code == 404, resp.text


def test_set_team_spend_limit_assert_error_matrix(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    _assert_error_matrix(
        http,
        method="put",
        url=f"/api/v1/admin/costs/limits/teams/{_TEAM_ID}",
        json_body={"daily_spend_limit": 5},
        patch_target="get_team",
        expected={(_PROG, 501), (_SQL, 503), (_RUNTIME, 500)},
    )


# ---------------------------------------------------------------------------
# GET/PUT /controls — error mapping + full update merge
# ---------------------------------------------------------------------------


def test_get_cost_controls_assert_error_matrix(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    _assert_error_matrix(
        http,
        method="get",
        url="/api/v1/admin/costs/controls",
        json_body=None,
        patch_target="list_teams",
        expected={(_PROG, 501), (_SQL, 503), (_RUNTIME, 500)},
    )


def test_update_cost_controls_unknown_org_returns_404(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    with ExitStack() as stack:
        stack.enter_context(patch(f"{_PREFIX}get_organisation", new=AsyncMock(return_value=None)))
        stack.enter_context(_rls_cm())
        resp = http.put("/api/v1/admin/costs/controls", json={"budget": 5})

    assert resp.status_code == 404, resp.text


def test_update_cost_controls_assert_error_matrix(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    _assert_error_matrix(
        http,
        method="put",
        url="/api/v1/admin/costs/controls",
        json_body={"budget": 5},
        patch_target="get_organisation",
        expected={(_PROG, 501), (_SQL, 503), (_RUNTIME, 500)},
    )


def _make_org() -> MagicMock:
    org = MagicMock()
    org.id = _ORG_ID
    org.daily_spend_limit = Decimal("10.00")
    org.max_run_cost_cents = 500
    org.spend_ceiling_cents = 10_000
    org.org_cumulative_spend_cents = 2_500
    org.settings_json = {"cost_controls": {"currency": "EUR", "billing_period": "annual"}}
    return org


def test_update_cost_controls_applies_all_fields(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    org = _make_org()
    teams_result = MagicMock()
    teams_result.items = []
    with ExitStack() as stack:
        stack.enter_context(patch(f"{_PREFIX}get_organisation", new=AsyncMock(return_value=org)))
        stack.enter_context(patch(f"{_PREFIX}list_teams", new=AsyncMock(return_value=teams_result)))
        stack.enter_context(_rls_cm())
        resp = http.put(
            "/api/v1/admin/costs/controls",
            json={
                "budget": 25.5,
                "max_run_cost": 3,
                "spend_ceiling": 90,
                "alert_thresholds": [50, 80],
                "circuit_breaker_enabled": True,
                "currency": "GBP",
                "billing_period": "quarterly",
            },
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["budget"] == 25.5
    assert body["max_run_cost"] == 3.0
    assert body["spend_ceiling"] == 90.0
    assert body["alert_thresholds"] == [50.0, 80.0]
    assert body["circuit_breaker_enabled"] is True
    assert body["currency"] == "GBP"
    assert body["billing_period"] == "quarterly"
    assert org.daily_spend_limit == Decimal("25.5")


def test_update_cost_controls_no_settings_only_leaves_org_untouched() -> None:
    """A request with only unset optional fields must not rewrite settings_json."""
    from modulo.api.routes.costs import UpdateCostControlsRequest, _apply_cost_control_updates

    org = MagicMock()
    org.daily_spend_limit = None
    org.settings_json = None
    req = UpdateCostControlsRequest(budget=None)
    _apply_cost_control_updates(org, req)
    assert org.settings_json is None


# ---------------------------------------------------------------------------
# GET/PUT /ceiling — error mapping + remaining-budget arithmetic
# ---------------------------------------------------------------------------


def test_get_spend_ceiling_assert_error_matrix(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    _assert_error_matrix(
        http,
        method="get",
        url="/api/v1/admin/costs/ceiling",
        json_body=None,
        patch_target="get_organisation",
        expected={(_PROG, 501), (_SQL, 503), (_RUNTIME, 500)},
    )


def test_get_spend_ceiling_computes_remaining(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    with ExitStack() as stack:
        stack.enter_context(patch(f"{_PREFIX}get_organisation", new=AsyncMock(return_value=_make_org())))
        stack.enter_context(_rls_cm())
        resp = http.get("/api/v1/admin/costs/ceiling")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["max_run_cost"] == 5.0
    assert body["spend_ceiling"] == 100.0
    assert body["org_cumulative_spend_usd"] == 25.0
    assert body["remaining_budget_usd"] == 75.0


def test_set_spend_ceiling_unknown_org_returns_404(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    with ExitStack() as stack:
        stack.enter_context(patch(f"{_PREFIX}get_organisation", new=AsyncMock(return_value=None)))
        stack.enter_context(_rls_cm())
        resp = http.put("/api/v1/admin/costs/ceiling", json={"spend_ceiling": 10})

    assert resp.status_code == 404, resp.text


def test_set_spend_ceiling_assert_error_matrix(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    _assert_error_matrix(
        http,
        method="put",
        url="/api/v1/admin/costs/ceiling",
        json_body={"spend_ceiling": 10},
        patch_target="get_organisation",
        expected={(_PROG, 501), (_SQL, 503), (_RUNTIME, 500)},
    )


def test_set_spend_ceiling_partial_update_keeps_other_ceiling(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    org = _make_org()
    with ExitStack() as stack:
        stack.enter_context(patch(f"{_PREFIX}get_organisation", new=AsyncMock(return_value=org)))
        stack.enter_context(_rls_cm())
        resp = http.put("/api/v1/admin/costs/ceiling", json={"spend_ceiling": None})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["spend_ceiling"] is None
    assert body["max_run_cost"] == 5.0


# ---------------------------------------------------------------------------
# POST /circuit-breaker/{pipeline_id}/reset
# ---------------------------------------------------------------------------


def test_reset_circuit_breaker_unknown_pipeline_returns_404(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    with ExitStack() as stack:
        stack.enter_context(patch(f"{_PREFIX}reset_pipeline_circuit_breaker", new=AsyncMock(return_value=False)))
        stack.enter_context(_rls_cm())
        resp = http.post(f"/api/v1/admin/costs/circuit-breaker/{_PIPELINE_ID}/reset")

    assert resp.status_code == 404, resp.text


def test_reset_circuit_breaker_assert_error_matrix(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    _assert_error_matrix(
        http,
        method="post",
        url=f"/api/v1/admin/costs/circuit-breaker/{_PIPELINE_ID}/reset",
        json_body=None,
        patch_target="reset_pipeline_circuit_breaker",
        expected={(_PROG, 501), (_SQL, 503), (_RUNTIME, 500)},
    )


def test_reset_circuit_breaker_happy_path(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    with ExitStack() as stack:
        stack.enter_context(patch(f"{_PREFIX}reset_pipeline_circuit_breaker", new=AsyncMock(return_value=True)))
        stack.enter_context(_rls_cm())
        resp = http.post(f"/api/v1/admin/costs/circuit-breaker/{_PIPELINE_ID}/reset")

    assert resp.status_code == 200, resp.text
    assert resp.json()["circuit_breaker_tripped"] is False


# ---------------------------------------------------------------------------
# GET /export — error mapping + CSV shape
# ---------------------------------------------------------------------------


def test_export_costs_assert_error_matrix(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    _assert_error_matrix(
        http,
        method="get",
        url="/api/v1/admin/costs/export",
        json_body=None,
        patch_target="get_cost_export_rows",
        expected={(_PROG, 501), (_SQL, 503), (_RUNTIME, 500)},
    )


def test_export_costs_streams_csv(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    rows = [
        {"entity_id": str(_TEAM_ID), "entity_name": "Team A", "total_spend_usd": 4.2, "total_runs": 9},
    ]
    with ExitStack() as stack:
        stack.enter_context(patch(f"{_PREFIX}get_cost_export_rows", new=AsyncMock(return_value=rows)))
        stack.enter_context(_rls_cm())
        resp = http.get("/api/v1/admin/costs/export", params={"period": "30d", "group_by": "team"})

    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/csv")
    assert "Team A" in resp.text
    assert "4.2" in resp.text


def test_export_pipeline_and_model_granularities_no_longer_422(client: tuple[TestClient, AsyncMock]) -> None:
    """``pipeline`` / ``model`` export granularities now stream real rows."""
    http, _session = client
    for group_by in ("pipeline", "model"):
        rows = [
            {
                "entity_id": f"entity-{group_by}",
                "entity_name": f"{group_by} spend",
                "total_spend_usd": 1.5,
                "total_runs": 2,
            }
        ]
        with ExitStack() as stack:
            stack.enter_context(patch(f"{_PREFIX}get_cost_export_rows", new=AsyncMock(return_value=rows)))
            stack.enter_context(_rls_cm())
            resp = http.get("/api/v1/admin/costs/export", params={"period": "7d", "group_by": group_by})

        assert resp.status_code == 200, f"group_by={group_by}: {resp.text}"
        assert f"{group_by} spend" in resp.text
        assert "1.5" in resp.text


# ---------------------------------------------------------------------------
# Scheduled reports — error mapping + 404
# ---------------------------------------------------------------------------


def _make_report() -> MagicMock:
    report = MagicMock()
    report.id = _REPORT_ID
    report.period = "daily"
    report.group_by = "team"
    report.format = "csv"
    report.recipients = ["ops@example.com"]
    report.schedule_type = "recurring"
    report.created_at = datetime(2025, 1, 1, tzinfo=UTC)
    return report


def test_create_report_assert_error_matrix(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    _assert_error_matrix(
        http,
        method="post",
        url="/api/v1/admin/costs/reports",
        json_body={"period": "daily", "group_by": "team", "recipients": ["ops@example.com"]},
        patch_target="create_scheduled_report",
        expected={(_PROG, 501), (_SQL, 503), (_RUNTIME, 500)},
    )


def test_create_report_happy_path(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    with ExitStack() as stack:
        stack.enter_context(patch(f"{_PREFIX}create_scheduled_report", new=AsyncMock(return_value=_make_report())))
        stack.enter_context(_rls_cm())
        resp = http.post(
            "/api/v1/admin/costs/reports",
            json={"period": "daily", "group_by": "team", "recipients": ["ops@example.com"]},
        )

    assert resp.status_code == 201, resp.text
    assert resp.json()["id"] == str(_REPORT_ID)


def test_list_reports_assert_error_matrix(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    _assert_error_matrix(
        http,
        method="get",
        url="/api/v1/admin/costs/reports",
        json_body=None,
        patch_target="list_scheduled_reports",
        expected={(_PROG, 501), (_SQL, 503), (_RUNTIME, 500)},
    )


def test_list_reports_happy_path(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    with ExitStack() as stack:
        stack.enter_context(patch(f"{_PREFIX}list_scheduled_reports", new=AsyncMock(return_value=[_make_report()])))
        stack.enter_context(_rls_cm())
        resp = http.get("/api/v1/admin/costs/reports")

    assert resp.status_code == 200, resp.text
    assert resp.json()[0]["id"] == str(_REPORT_ID)


def test_delete_report_assert_error_matrix(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    _assert_error_matrix(
        http,
        method="delete",
        url=f"/api/v1/admin/costs/reports/{_REPORT_ID}",
        json_body=None,
        patch_target="delete_scheduled_report",
        expected={(_PROG, 501), (_SQL, 503), (_RUNTIME, 500)},
    )


def test_delete_report_unknown_returns_404(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    with ExitStack() as stack:
        stack.enter_context(patch(f"{_PREFIX}delete_scheduled_report", new=AsyncMock(return_value=False)))
        stack.enter_context(_rls_cm())
        resp = http.delete(f"/api/v1/admin/costs/reports/{_REPORT_ID}")

    assert resp.status_code == 404, resp.text


def test_delete_report_happy_path(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    with ExitStack() as stack:
        stack.enter_context(patch(f"{_PREFIX}delete_scheduled_report", new=AsyncMock(return_value=True)))
        stack.enter_context(_rls_cm())
        resp = http.delete(f"/api/v1/admin/costs/reports/{_REPORT_ID}")

    assert resp.status_code == 204, resp.text


# ---------------------------------------------------------------------------
# Anomalies — detection/merge branches + error mapping
# ---------------------------------------------------------------------------


def test_build_rolling_anomalies_detects_spike_and_skips_flat() -> None:
    from modulo.api.routes.costs import _build_rolling_anomalies

    base = [(datetime(2025, 1, 1, tzinfo=UTC).date(), 1.0)] * 7
    flat = [*base, (datetime(2025, 1, 2, tzinfo=UTC).date(), 1.0)]
    assert not _build_rolling_anomalies(flat)

    spiky = [*base, (datetime(2025, 1, 2, tzinfo=UTC).date(), 5.0)]
    anomalies = _build_rolling_anomalies(spiky)
    assert len(anomalies) == 1
    assert anomalies[0]["amount"] == 5.0
    assert anomalies[0]["percent_above"] == 400.0


def _make_stored_anomaly(date_value: object = None) -> MagicMock:
    a = MagicMock()
    a.id = _ANOMALY_ID
    a.anomaly_date = date_value if date_value is not None else datetime(2025, 1, 2, tzinfo=UTC).date()
    a.pipeline_id = _PIPELINE_ID
    a.amount = 5.0
    a.baseline = 1.0
    a.percent_above = 400.0
    a.dismissed = True
    return a


def test_merge_anomalies_keeps_stored_dismissals_and_filters_stored_only() -> None:
    from modulo.api.routes.costs import _merge_anomalies

    detected = [
        {
            "id": "",
            "anomaly_date": "2025-01-02",
            "pipeline_id": None,
            "amount": 5.0,
            "baseline": 1.0,
            "percent_above": 400.0,
            "dismissed": False,
        }
    ]
    other_date = datetime(2025, 2, 1, tzinfo=UTC).date()
    other_row = _make_stored_anomaly(other_date)
    other_row.dismissed = False
    lapsed_dismissed_date = datetime(2025, 3, 1, tzinfo=UTC).date()
    merged = _merge_anomalies(
        detected,
        [_make_stored_anomaly(), other_row, _make_stored_anomaly(lapsed_dismissed_date)],
    )

    assert merged[0]["dismissed"] is True
    assert [m["anomaly_date"] for m in merged] == ["2025-01-02", str(other_date)]


def test_get_anomalies_assert_error_matrix(client: tuple[TestClient, AsyncMock]) -> None:
    http, session = client
    for exc, expected in [(_PROG, 501), (_SQL, 503), (_RUNTIME, 500)]:
        session.execute = AsyncMock(side_effect=exc)
        with _rls_cm():
            resp = http.get("/api/v1/admin/costs/anomalies")
        session.execute = _make_session().execute
        assert resp.status_code == expected, resp.text


def test_get_anomalies_happy_path_merges_detected_and_stored(client: tuple[TestClient, AsyncMock]) -> None:
    http, session = client
    today = datetime.now(UTC).date()
    spike_date = today - timedelta(days=2)
    counts = MagicMock()
    counts.all = MagicMock(
        return_value=[
            MagicMock(run_date=today - timedelta(days=9), daily_spend=1.0),
            MagicMock(run_date=today - timedelta(days=8), daily_spend=1.0),
            MagicMock(run_date=today - timedelta(days=7), daily_spend=1.0),
            MagicMock(run_date=today - timedelta(days=6), daily_spend=1.0),
            MagicMock(run_date=today - timedelta(days=5), daily_spend=1.0),
            MagicMock(run_date=today - timedelta(days=4), daily_spend=1.0),
            MagicMock(run_date=today - timedelta(days=3), daily_spend=1.0),
            MagicMock(run_date=spike_date, daily_spend=5.0),
        ]
    )
    session.execute = AsyncMock(return_value=counts)
    stored = [
        _make_stored_anomaly(spike_date),
        _make_stored_anomaly(today - timedelta(days=20)),
    ]
    stored[1].dismissed = False
    with ExitStack() as stack:
        stack.enter_context(patch(f"{_PREFIX}list_anomalies", new=AsyncMock(return_value=stored)))
        stack.enter_context(_rls_cm())
        resp = http.get("/api/v1/admin/costs/anomalies")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body) == 2
    assert body[0]["dismissed"] is True
    assert body[1]["anomaly_date"] == str(today - timedelta(days=20))


def test_fresh_detection_is_persisted_and_dismissible(client: tuple[TestClient, AsyncMock]) -> None:
    """First sight of a detection must be stored so it gets a real id.

    Without persistence a fresh detection carries an empty ``id`` and could
    never be targeted by POST /anomalies/dismiss/{id}; after this pass every
    detected day is recorded via ``record_or_get_anomaly`` and returned with
    its stored id.
    """
    http, session = client
    today = datetime.now(UTC).date()
    spike_date = today - timedelta(days=1)
    counts = MagicMock()
    counts.all = MagicMock(
        return_value=[
            MagicMock(run_date=today - timedelta(days=8), daily_spend=1.0),
            MagicMock(run_date=today - timedelta(days=7), daily_spend=1.0),
            MagicMock(run_date=today - timedelta(days=6), daily_spend=1.0),
            MagicMock(run_date=today - timedelta(days=5), daily_spend=1.0),
            MagicMock(run_date=today - timedelta(days=4), daily_spend=1.0),
            MagicMock(run_date=today - timedelta(days=3), daily_spend=1.0),
            MagicMock(run_date=today - timedelta(days=2), daily_spend=1.0),
            MagicMock(run_date=spike_date, daily_spend=5.0),
        ]
    )
    session.execute = AsyncMock(return_value=counts)

    persisted = MagicMock()
    persisted.id = uuid.uuid4()
    persisted.anomaly_date = spike_date
    persisted.pipeline_id = None
    persisted.amount = 5.0
    persisted.baseline = 1.0
    persisted.percent_above = 400.0
    persisted.dismissed = False

    with ExitStack() as stack:
        stack.enter_context(patch(f"{_PREFIX}list_anomalies", new=AsyncMock(return_value=[])))
        stack.enter_context(patch(f"{_PREFIX}record_or_get_anomaly", new=AsyncMock(return_value=persisted)))
        stack.enter_context(_rls_cm())
        resp = http.get("/api/v1/admin/costs/anomalies")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body) == 1
    assert body[0]["id"] == str(persisted.id)
    assert body[0]["anomaly_date"] == str(spike_date)
    assert body[0]["dismissed"] is False


def test_dismiss_anomaly_assert_error_matrix(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    _assert_error_matrix(
        http,
        method="post",
        url=f"/api/v1/admin/costs/anomalies/dismiss/{_ANOMALY_ID}",
        json_body=None,
        patch_target="dismiss_anomaly",
        expected={(_PROG, 501), (_SQL, 503), (_RUNTIME, 500)},
    )


def test_dismiss_anomaly_unknown_returns_404(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    with ExitStack() as stack:
        stack.enter_context(patch(f"{_PREFIX}dismiss_anomaly", new=AsyncMock(return_value=False)))
        stack.enter_context(_rls_cm())
        resp = http.post(f"/api/v1/admin/costs/anomalies/dismiss/{_ANOMALY_ID}")

    assert resp.status_code == 404, resp.text


def test_dismiss_anomaly_happy_path(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    with ExitStack() as stack:
        stack.enter_context(patch(f"{_PREFIX}dismiss_anomaly", new=AsyncMock(return_value=True)))
        stack.enter_context(_rls_cm())
        resp = http.post(f"/api/v1/admin/costs/anomalies/dismiss/{_ANOMALY_ID}")

    assert resp.status_code == 204, resp.text
