"""Step definitions for Home Dashboard Summary BDD scenarios (PRD §8.20).

Wires the scenarios in ``dashboard_summary.feature`` to the real
``GET /api/v1/dashboard/summary`` route (``modulo/api/routes/dashboard.py``)
through the shared TestClient + mock-session pattern used by
``test_hitl_trends_steps.py``. The session dispatches the route's org
aggregation queries (active pipelines, status counts, teams, eval stats,
daily trend, recent runs, config warnings, and the optional ``days`` period
block) on SQL text, so the scenarios assert the actual API contract — widget
shape, idle folding of pending/claimed into ``idle``, single-counted
``total_runs``, the additive ``period`` block, and the 1..90 ``days`` bound.
The story Redis cache is neutralised to keep the assertions deterministic.
"""

import uuid
from collections.abc import AsyncGenerator, Generator
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient
from pytest_bdd import given, parsers, scenarios, then, when

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_tenant_user
from modulo.auth.jwt import TenantPrincipal
from modulo.settings import Settings, get_settings

scenarios("dashboard_summary.feature")

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_ACCOUNT_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_TEAM_ID = uuid.UUID("00000000-0000-0000-0000-000000000011")

_TRACKED = ("running", "awaiting_human", "failed", "idle")
_IDLE = ("pending", "claimed", "hitl_parked")


class _Row:
    """Simulates a SQLAlchemy result row with named attribute access."""

    def __init__(self, **kwargs: object) -> None:
        for k, v in kwargs.items():
            setattr(self, k, v)


class _Result:
    """Simulates a SQLAlchemy result proxy for chain calls."""

    def __init__(self, rows: list[_Row] | None = None, scalar_one_val: object = 0) -> None:
        self._rows = rows if rows is not None else []
        self._scalar_one = scalar_one_val

    def scalar_one(self) -> object:
        return self._scalar_one

    def scalar_one_or_none(self) -> object:
        return self._scalar_one

    def one(self) -> _Row:
        if self._rows:
            return self._rows[0]
        return _Row(total=0, passed=0)

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


def _make_summary_session(
    *,
    status_counts: dict[str, int] | None = None,
    teams: list[_Row] | None = None,
    eval_total: int = 100,
    eval_passed: int = 75,
    active_pipelines: int = 2,
    daily: list[_Row] | None = None,
    current_facts: dict[str, object] | None = None,
    previous_facts: dict[str, object] | None = None,
) -> AsyncMock:
    """AsyncSession double dispatching the summary route's queries on SQL text.

    Mirrors the database shapes the route loads: the raw ``runs.status``
    group-by (folded into ``idle`` by the endpoint), the team list, the eval
    totals plus per-team-pipeline breakdown, the org-daily-run-count ledger
    trend, recent runs, model-backend count (→ config warnings), and — for the
    ``days`` path — the ``run_daily_facts`` windows / ledger spend / eval-rate
    windows. Returns the empty/zero shape by default so un-seeded queries are
    deterministic rather than erroring the strict mock.
    """
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

    status_rows = [_Row(status=s, cnt=c) for s, c in (status_counts or {}).items()]
    daily_rows = daily or []
    facts_states = {"i": 0}

    default_facts = {"total": 10, "active_pipelines": 3, "tokens": 5000, "avg_duration_ms": 120.0, "complete": 9}
    facts_sources = [
        current_facts if current_facts is not None else default_facts,
        previous_facts if previous_facts is not None else default_facts,
    ]

    def _facts_result() -> _Result:
        facts = facts_sources[facts_states["i"] % len(facts_sources)]
        facts_states["i"] += 1
        return _Result(rows=[_Row(**facts)])

    def _execute_side_effect(stmt: object, *_args: object, **_kwargs: object) -> _Result:
        text = str(stmt).lower()
        if "authz_enforce" in text:
            return _Result(scalar_one_val=None)
        if "run_daily_facts" in text and "group by" not in text:
            return _facts_result()
        if "run_daily_facts" in text:
            return _Result()
        if "org_daily_run_counts" in text and "group by" not in text:
            return _Result(scalar_one_val=0)
        if "org_daily_run_counts" in text:
            return _Result(rows=daily_rows)
        if "model_backends" in text:
            return _Result(scalar_one_val=0)
        if "pipeline_name" in text:
            return _Result()
        if "from teams" in text:
            if teams:
                return _Result(rows=teams)
            return _Result(rows=[_Row(id=str(_TEAM_ID), name="Platform")])
        if "owner_team_id" in text:
            return _Result()
        if "group by runs.status" in text:
            return _Result(rows=status_rows)
        if "from pipelines" in text and "archived_at" in text:
            return _Result(scalar_one_val=active_pipelines)
        if "eval_results" in text:
            if "join runs" in text or "group by" in text or "cast(" in text:
                return _Result()
            if "evaluated_at" in text:
                return _Result(rows=[_Row(total=eval_total, passed=eval_passed)])
            return _Result(rows=[_Row(total=eval_total, passed=eval_passed)])
        return _Result()

    session.execute = AsyncMock(side_effect=_execute_side_effect)
    return session


def _make_admin_client(session: AsyncMock | None = None) -> Generator[TestClient, None, None]:
    """Build a TestClient with an admin tenant principal and mock session."""
    if session is None:
        session = _make_summary_session()
    with (
        patch("modulo.api.routes.dashboard._get_cached_dashboard", new=AsyncMock(return_value=None)),
        patch("modulo.api.routes.dashboard._set_cached_dashboard", new=AsyncMock()),
    ):
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


def _capture(url: str, request: Any, session: AsyncMock | None = None) -> None:
    client_gen = _make_admin_client(session)
    client = next(client_gen)
    try:
        resp = client.get(url)
    finally:
        client_gen.close()
    request.node._resp = resp


# ---------------------------------------------------------------------------
# Given
# ---------------------------------------------------------------------------


@given("I am authenticated as an admin")
def _given_auth_admin() -> None:
    """No-op — the ``when`` steps build an admin-principal TestClient."""


@given(parsers.re(r'the org has runs with statuses (?P<spec>[^"]+)'))
def _given_org_statuses(spec: str, request: Any) -> None:
    counts: dict[str, int] = {}
    for item in spec.split(","):
        status, count = item.split("=", 1)
        counts[status.strip()] = int(count.strip())
    request.node._ctx = {"status_counts": counts}


def _ctx(request: Any) -> dict[str, Any]:
    if not hasattr(request.node, "_ctx"):
        request.node._ctx = {}
    return cast("dict[str, Any]", request.node._ctx)


# ---------------------------------------------------------------------------
# When
# ---------------------------------------------------------------------------


@when(parsers.re(r"I request GET /api/v1/dashboard/summary(?P<query>[^ ]*)"))
def _when_request_summary(request: Any, query: str = "") -> None:
    ctx = _ctx(request)
    url = "/api/v1/dashboard/summary" + query
    session = None
    if ctx.get("status_counts"):
        session = _make_summary_session(status_counts=ctx["status_counts"])
    _capture(url, request, session)


# ---------------------------------------------------------------------------
# Then
# ---------------------------------------------------------------------------


def _body(request: Any) -> dict[str, Any]:
    resp = request.node._resp
    body = resp.json()
    assert isinstance(body, dict), f"Expected a JSON object body, got {type(body)}"
    return body


@then("the summary contains total_runs, active_pipelines, and run_counts_by_status")
def _then_widget_keys(request: Any) -> None:
    body = _body(request)
    assert "total_runs" in body
    assert "active_pipelines" in body
    counts = body["run_counts_by_status"]
    for status in _TRACKED:
        assert status in counts, f"run_counts_by_status missing '{status}': {sorted(counts)}"


@then("the summary contains per-team metrics with run counts")
def _then_team_metrics(request: Any) -> None:
    body = _body(request)
    assert isinstance(body["teams"], list)
    for team in body["teams"]:
        assert "id" in team, f"team metric missing 'id': {team}"
        assert "name" in team, f"team metric missing 'name': {team}"
        assert "total_runs" in team, f"team metric missing 'total_runs': {team}"
        assert "run_counts_by_status" in team, f"team metric missing 'run_counts_by_status': {team}"


@then("the summary contains an eval pass rate with per-pipeline detail")
def _then_eval_pass_rate(request: Any) -> None:
    er = _body(request)["eval_pass_rate"]
    for field in ("overall_pass_rate", "total_evals", "passed_evals", "per_pipeline"):
        assert field in er, f"eval_pass_rate missing '{field}'"


@then("the summary contains a 7-day trend with run counts and spend")
def _then_trend(request: Any) -> None:
    trend = _body(request)["trend"]
    assert isinstance(trend, list) and len(trend) == 7
    for point in trend:
        for field in ("date", "run_count", "eval_pass_rate", "token_spend_usd"):
            assert field in point, f"trend point missing '{field}': {point}"


@then("the summary contains recent runs and config warnings")
def _then_recent_and_warnings(request: Any) -> None:
    body = _body(request)
    assert isinstance(body["recent_runs"], list)
    assert isinstance(body["config_warnings"], list)


@then(parsers.parse("the summary run_counts_by_status has running={running:d}, failed={failed:d}, and idle={idle:d}"))
def _then_status_counts(request: Any, running: int, failed: int, idle: int) -> None:
    counts = _body(request)["run_counts_by_status"]
    assert counts["running"] == running, f"running={counts['running']}, expected {running}"
    assert counts["failed"] == failed, f"failed={counts['failed']}, expected {failed}"
    assert counts["idle"] == idle, f"idle={counts['idle']}, expected {idle}"


@then("total_runs counts pending/claimed only once and equals 10")
def _then_total_runs_single_count(request: Any) -> None:
    body = _body(request)
    counts = body["run_counts_by_status"]
    assert body["total_runs"] == sum(v for k, v in counts.items() if k not in _IDLE)
    assert body["total_runs"] == 10


@then(parsers.parse("the summary period block reports days={days:d} with current/previous/delta_pct metrics"))
def _then_period_block(request: Any, days: int) -> None:
    period = _body(request)["period"]
    assert period["days"] == days
    metrics = period["metrics"]
    assert "total_runs" in metrics
    for name, metric in metrics.items():
        if name == "run_counts_by_status":
            for per_status in metric.values():
                for field in ("current", "previous", "delta_pct"):
                    assert field in per_status, f"period status metric missing '{field}': {per_status}"
        else:
            for field in ("current", "previous", "delta_pct"):
                assert field in metric, f"period metric missing '{field}': {metric}"
