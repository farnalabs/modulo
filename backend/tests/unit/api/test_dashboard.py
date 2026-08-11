"""Unit tests for /api/v1/dashboard/summary and /api/v1/dashboard/trends."""

import uuid
from collections.abc import AsyncGenerator, Callable, Generator
from datetime import UTC, date, datetime, timedelta
from typing import Any, ClassVar
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.api.routes.dashboard import (
    _compute_period_metrics,
    _facts_status_counts,
    _facts_window,
)
from modulo.auth.dependencies import get_current_tenant_user, get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal, TenantPrincipal
from modulo.settings import Settings, get_settings
from tests.unit.api.mock_session import configure_mock_session

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")


@pytest.fixture(autouse=True)
def _disable_dashboard_redis_cache() -> Generator[None, None, None]:
    """Neutralise the dashboard summary Redis cache for the whole module.

    ``_get_cached_dashboard`` / ``_set_cached_dashboard`` read ``get_settings()``
    directly (not the overridden dependency), so when a live Redis is reachable
    at the configured ``REDIS_URL`` a cached summary from an earlier test
    (TTL 60s) is returned to later tests — poisoning any test that supplies
    custom mock window kwargs with the default values cached first. Disabling
    the cache makes every test deterministic regardless of the environment.
    """
    with (
        patch("modulo.api.routes.dashboard._get_cached_dashboard", new=AsyncMock(return_value=None)),
        patch("modulo.api.routes.dashboard._set_cached_dashboard", new=AsyncMock()),
    ):
        yield


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


class _MockRow:
    """Simulates a SQLAlchemy result row with named attribute access."""

    def __init__(self, **kwargs: object) -> None:
        for k, v in kwargs.items():
            setattr(self, k, v)


class _MockResult:
    """Simulates a SQLAlchemy result proxy for chain calls."""

    def __init__(self, scalar_one_val: object = 42, rows: object | None = None) -> None:
        self._scalar_one = scalar_one_val
        self._rows = rows if rows is not None else []

    def scalar_one(self) -> object:
        return self._scalar_one

    def scalar_one_or_none(self) -> object:
        return self._scalar_one

    def one(self) -> "_MockRow":
        """Return the first row, or a default row if none exist."""
        if hasattr(self._rows, "__iter__"):
            rows_list = list(self._rows)
            if rows_list:
                return rows_list[0]
        return _MockRow(total=100, passed=75)

    def scalars(self) -> "_MockResult":
        return self

    def all(self) -> list[object]:
        return list(self._rows) if hasattr(self._rows, "__iter__") else []

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(self._rows if hasattr(self._rows, "__iter__") else [])


class _CapturingSession:
    """Fake AsyncSession that records every statement passed to execute()."""

    def __init__(self, result: "_MockResult") -> None:
        self._result = result
        self.statements: list[Any] = []

    async def execute(self, stmt: Any, *_args: object, **_kwargs: object) -> "_MockResult":
        self.statements.append(stmt)
        return self._result


def _make_mock_session() -> AsyncMock:
    session = configure_mock_session(AsyncMock())
    authz_result = MagicMock()
    authz_result.scalar_one_or_none = MagicMock(return_value=True)
    session.execute = AsyncMock(return_value=authz_result)
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)

    def _execute_side_effect(*_args: object, **_kwargs: object) -> _MockResult:
        return _MockResult()

    session.execute = AsyncMock(side_effect=_execute_side_effect)
    return session


def _make_period_mock_session(
    *,
    current_facts: dict[str, int | float | None] | None = None,
    previous_facts: dict[str, int | float | None] | None = None,
    current_status: dict[str, int] | None = None,
    previous_status: dict[str, int] | None = None,
    current_spend: float = 0.0,
    previous_spend: float = 0.0,
    current_eval: tuple[int, int] = (0, 0),
    previous_eval: tuple[int, int] = (0, 0),
) -> AsyncMock:
    """Session mock whose execute() dispatches the period-scoped dashboard queries.

    ``run_daily_facts`` queries are matched by their aggregate labels; the
    ``current``/``previous`` window for each period source is resolved by call
    order (the route queries current-then-previous, deterministic per request).
    All other queries return benign empty/default results.
    """
    default_facts: dict[str, int | float | None] = {
        "total": 10,
        "active_pipelines": 2,
        "tokens": 1000,
        "avg_duration_ms": 150.0,
        "complete": 8,
    }
    facts_results: list[dict[str, int | float | None]] = [
        current_facts if current_facts is not None else default_facts,
        previous_facts if previous_facts is not None else default_facts,
    ]
    status_results: list[dict[str, int]] = [current_status or {}, previous_status or {}]
    spend_results: list[float] = [current_spend, previous_spend]
    eval_results: list[tuple[int, int]] = [current_eval, previous_eval]
    counters = {"facts": 0, "status": 0, "spend": 0, "eval": 0}

    session = configure_mock_session(AsyncMock())
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)

    def _facts_result() -> _MockResult:
        row = facts_results[counters["facts"]]
        counters["facts"] += 1
        return _MockResult(rows=[_MockRow(**row)])

    def _status_result() -> _MockResult:
        mapping = status_results[counters["status"]]
        counters["status"] += 1
        return _MockResult(rows=[_MockRow(status=status, cnt=cnt) for status, cnt in mapping.items()])

    def _spend_result() -> _MockResult:
        value = spend_results[counters["spend"]]
        counters["spend"] += 1
        return _MockResult(scalar_one_val=value)

    def _eval_result() -> _MockResult:
        total, passed = eval_results[counters["eval"]]
        counters["eval"] += 1
        return _MockResult(rows=[_MockRow(total=total, passed=passed)])

    def _execute_side_effect(stmt: object, *_args: object, **_kwargs: object) -> _MockResult:
        text = str(stmt)
        if "model_backends" in text:
            return _MockResult(scalar_one_val=0)
        if "run_daily_facts" in text:
            if "active_pipelines" in text:
                return _facts_result()
            return _status_result()
        if "org_daily_run_counts" in text:
            if "GROUP BY" in text:
                return _MockResult()
            return _spend_result()
        if "eval_results" in text:
            if "JOIN runs" in text or "GROUP BY" in text:
                return _MockResult()
            if "evaluated_at" in text:
                return _eval_result()
            return _MockResult(rows=[_MockRow(total=0, passed=0)])
        if "FROM teams" in text:
            return _MockResult()
        if "pipeline_cnt" in text:
            return _MockResult()
        if "pipeline_name" in text:
            return _MockResult()
        if "archived_at" in text:
            return _MockResult(scalar_one_val=0)
        return _MockResult()

    session.execute = AsyncMock(side_effect=_execute_side_effect)
    return session


@pytest.fixture
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
    app.dependency_overrides[get_current_tenant_user] = lambda: TenantPrincipal(
        username="tenant", organisation_id=_ORG_ID, account_id=_USER_ID, org_role="admin"
    )
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def period_client() -> Generator[Callable[..., TestClient], None, None]:
    """Factory fixture — returns a TestClient wired to a period-aware mock session.

    Keyword arguments are forwarded to ``_make_period_mock_session`` so each
    test can control the ``run_daily_facts`` window, ledger spend, and eval
    rate values returned for the ``/summary?days=N`` period block.
    """

    def _build(**period_kwargs: object) -> TestClient:
        mock_session = _make_period_mock_session(**period_kwargs)

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
        app.dependency_overrides[get_current_tenant_user] = lambda: TenantPrincipal(
            username="tenant", organisation_id=_ORG_ID, account_id=_USER_ID, org_role="admin"
        )
        mock_plan = MagicMock()
        mock_plan.feature_enabled.return_value = True
        app.dependency_overrides[get_plan_context] = lambda: mock_plan
        return TestClient(app)

    yield _build
    app.dependency_overrides.clear()


@pytest.fixture
def unauth_client() -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_settings] = _make_settings
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


class TestDashboardSummary:
    """GET /api/v1/dashboard/summary"""

    def test_returns_summary(self, client: TestClient) -> None:
        response = client.get("/api/v1/dashboard/summary")
        assert response.status_code == 200
        body = response.json()
        assert "total_runs" in body
        assert "active_pipelines" in body
        assert "run_counts_by_status" in body
        counts = body["run_counts_by_status"]
        for status in ("running", "awaiting_human", "failed", "idle"):
            assert status in counts
            assert isinstance(counts[status], int)

    def test_includes_team_metrics(self, client: TestClient) -> None:
        response = client.get("/api/v1/dashboard/summary")
        assert response.status_code == 200
        body = response.json()
        assert "teams" in body
        assert isinstance(body["teams"], list)

    def test_includes_eval_pass_rate(self, client: TestClient) -> None:
        response = client.get("/api/v1/dashboard/summary")
        assert response.status_code == 200
        body = response.json()
        assert "eval_pass_rate" in body
        if body["eval_pass_rate"] is not None:
            er = body["eval_pass_rate"]
            assert "overall_pass_rate" in er
            assert "total_evals" in er
            assert "passed_evals" in er
            assert "per_pipeline" in er

    def test_includes_trend(self, client: TestClient) -> None:
        response = client.get("/api/v1/dashboard/summary")
        assert response.status_code == 200
        body = response.json()
        assert "trend" in body
        trend = body["trend"]
        assert isinstance(trend, list)
        for point in trend:
            assert "date" in point
            assert "run_count" in point
            assert "eval_pass_rate" in point
            assert "token_spend_usd" in point

    def test_trend_is_7_days(self, client: TestClient) -> None:
        response = client.get("/api/v1/dashboard/summary")
        assert response.status_code == 200
        body = response.json()
        assert len(body["trend"]) == 7

    def test_all_new_fields_present(self, client: TestClient) -> None:
        response = client.get("/api/v1/dashboard/summary")
        assert response.status_code == 200
        body = response.json()
        expected = {
            "total_runs",
            "active_pipelines",
            "run_counts_by_status",
            "teams",
            "eval_pass_rate",
            "trend",
            "recent_runs",
            "config_warnings",
        }
        assert set(body.keys()) == expected

    def test_config_warnings_is_list(self, client: TestClient) -> None:
        response = client.get("/api/v1/dashboard/summary")
        assert response.status_code == 200
        body = response.json()
        assert "config_warnings" in body
        assert isinstance(body["config_warnings"], list)

    def test_requires_auth(self, unauth_client: TestClient) -> None:
        response = unauth_client.get("/api/v1/dashboard/summary")
        assert response.status_code in (401, 403)


class TestDashboardSummaryPeriod:
    """GET /api/v1/dashboard/summary?days=N — period-scoped block (FAR-92)."""

    _PERIOD_METRIC_NAMES: ClassVar[frozenset[str]] = frozenset(
        {
            "total_runs",
            "active_pipelines",
            "run_counts_by_status",
            "tokens",
            "success_rate",
            "avg_duration_ms",
            "eval_pass_rate",
            "spend",
        }
    )

    def _period_metrics(self, client: TestClient, days: str = "7") -> dict[str, object]:
        response = client.get(f"/api/v1/dashboard/summary?days={days}")
        assert response.status_code == 200
        period = response.json()["period"]
        assert period["days"] == int(days)
        metrics = period["metrics"]
        assert set(metrics.keys()) == self._PERIOD_METRIC_NAMES
        return metrics

    def test_days_7_returns_period_block(self, period_client: Callable[..., TestClient]) -> None:
        client = period_client()
        metrics = self._period_metrics(client)
        for name, metric in metrics.items():
            if name == "run_counts_by_status":
                assert set(metric.keys()) == {"running", "awaiting_human", "failed", "idle"}
                for status_metric in metric.values():
                    assert set(status_metric.keys()) == {"current", "previous", "delta_pct"}
            else:
                assert set(metric.keys()) == {"current", "previous", "delta_pct"}
        assert metrics["success_rate"]["current"] == 80.0

    def test_days_1_3_30_90_accepted(self, period_client: Callable[..., TestClient]) -> None:
        for days in ("1", "3", "30", "90"):
            # Fresh client per request: the mock session's current/previous
            # window counters are consumed once per request.
            response = period_client().get(f"/api/v1/dashboard/summary?days={days}")
            assert response.status_code == 200
            assert response.json()["period"]["days"] == int(days)

    def test_arbitrary_days_in_range_accepted(self, period_client: Callable[..., TestClient]) -> None:
        # Any 1..90 is valid now (matching /trends), not just {1, 7, 30, 90}.
        for days in ("2", "3", "5", "45"):
            # Fresh client per request: the mock session's current/previous
            # window counters are consumed once per request.
            response = period_client().get(f"/api/v1/dashboard/summary?days={days}")
            assert response.status_code == 200
            assert response.json()["period"]["days"] == int(days)

    def test_out_of_range_days_rejected(self, client: TestClient) -> None:
        for days in ("0", "91", "-1"):
            response = client.get(f"/api/v1/dashboard/summary?days={days}")
            assert response.status_code == 422

    def test_period_absent_without_days(self, client: TestClient) -> None:
        response = client.get("/api/v1/dashboard/summary")
        assert response.status_code == 200
        assert "period" not in response.json()

    def test_delta_pct_null_when_previous_zero(self, period_client: Callable[..., TestClient]) -> None:
        client = period_client(
            current_facts={"total": 12, "active_pipelines": 2, "tokens": 1200, "avg_duration_ms": 150.0, "complete": 9},
            previous_facts={
                "total": 0,
                "active_pipelines": 0,
                "tokens": 0,
                "avg_duration_ms": None,
                "complete": 0,
            },
        )
        metrics = self._period_metrics(client, days="30")
        assert metrics["total_runs"]["previous"] == 0
        assert metrics["total_runs"]["delta_pct"] is None

    def test_delta_pct_null_when_current_null(self, period_client: Callable[..., TestClient]) -> None:
        # Empty current window → success_rate has no value → delta is undefined.
        client = period_client(
            current_facts={"total": 0, "active_pipelines": 0, "tokens": 0, "avg_duration_ms": None, "complete": 0}
        )
        metrics = self._period_metrics(client)
        assert metrics["success_rate"]["current"] is None
        assert metrics["success_rate"]["delta_pct"] is None

    def test_eval_pass_rate_null_without_evals(self, period_client: Callable[..., TestClient]) -> None:
        client = period_client(current_eval=(0, 0), previous_eval=(5, 4))
        metrics = self._period_metrics(client)
        assert metrics["eval_pass_rate"]["current"] is None
        assert metrics["eval_pass_rate"]["delta_pct"] is None

    def test_success_rate_is_complete_over_total(self, period_client: Callable[..., TestClient]) -> None:
        client = period_client(
            current_facts={"total": 10, "active_pipelines": 3, "tokens": 500, "avg_duration_ms": 200.0, "complete": 8}
        )
        metrics = self._period_metrics(client)
        assert metrics["total_runs"]["current"] == 10
        assert metrics["active_pipelines"]["current"] == 3
        assert metrics["tokens"]["current"] == 500
        assert metrics["avg_duration_ms"]["current"] == 200.0
        assert metrics["success_rate"]["current"] == 80.0

    def test_delta_pct_computed_between_windows(self, period_client: Callable[..., TestClient]) -> None:
        client = period_client(
            current_facts={"total": 12, "active_pipelines": 2, "tokens": 1200, "avg_duration_ms": 150.0, "complete": 9},
            previous_facts={
                "total": 10,
                "active_pipelines": 2,
                "tokens": 1000,
                "avg_duration_ms": 150.0,
                "complete": 8,
            },
        )
        metrics = self._period_metrics(client)
        assert metrics["total_runs"]["current"] == 12
        assert metrics["total_runs"]["previous"] == 10
        assert metrics["total_runs"]["delta_pct"] == 20.0

    def test_period_spend_uses_ledger_window(self, period_client: Callable[..., TestClient]) -> None:
        client = period_client(current_spend=15.5, previous_spend=10.0)
        metrics = self._period_metrics(client)
        assert metrics["spend"]["current"] == 15.5
        assert metrics["spend"]["previous"] == 10.0
        assert metrics["spend"]["delta_pct"] == 55.0


class TestDashboardTrends:
    """GET /api/v1/dashboard/trends"""

    def test_returns_trend_data(self, client: TestClient) -> None:
        response = client.get("/api/v1/dashboard/trends?days=7")
        assert response.status_code == 200
        body = response.json()
        assert "days" in body
        assert "run_counts" in body
        assert "eval_pass_rates" in body
        assert "token_spend" in body
        assert body["days"] == 7

    def test_defaults_to_7_days(self, client: TestClient) -> None:
        response = client.get("/api/v1/dashboard/trends")
        assert response.status_code == 200
        body = response.json()
        assert body["days"] == 7

    def test_accepts_30_days(self, client: TestClient) -> None:
        response = client.get("/api/v1/dashboard/trends?days=30")
        assert response.status_code == 200
        assert response.json()["days"] == 30

    def test_accepts_90_days(self, client: TestClient) -> None:
        response = client.get("/api/v1/dashboard/trends?days=90")
        assert response.status_code == 200
        assert response.json()["days"] == 90

    def test_rejects_invalid_days(self, client: TestClient) -> None:
        response = client.get("/api/v1/dashboard/trends?days=0")
        assert response.status_code == 422

    def test_rejects_too_many_days(self, client: TestClient) -> None:
        response = client.get("/api/v1/dashboard/trends?days=91")
        assert response.status_code == 422

    def test_requires_auth(self, unauth_client: TestClient) -> None:
        response = unauth_client.get("/api/v1/dashboard/trends")
        assert response.status_code in (401, 403)

    def test_run_counts_structure(self, client: TestClient) -> None:
        response = client.get("/api/v1/dashboard/trends?days=7")
        assert response.status_code == 200
        body = response.json()
        for entry in body["run_counts"]:
            assert "date" in entry
            assert "run_count" in entry

    def test_eval_pass_rates_structure(self, client: TestClient) -> None:
        response = client.get("/api/v1/dashboard/trends?days=7")
        assert response.status_code == 200
        body = response.json()
        for entry in body["eval_pass_rates"]:
            assert "date" in entry
            assert "total_evals" in entry
            assert "passed_evals" in entry
            assert "pass_rate" in entry

    def test_token_spend_structure(self, client: TestClient) -> None:
        response = client.get("/api/v1/dashboard/trends?days=7")
        assert response.status_code == 200
        body = response.json()
        for entry in body["token_spend"]:
            assert "date" in entry
            assert "total_spend_usd" in entry

    def test_hitl_volume_present(self, client: TestClient) -> None:
        """HITL metrics are present in trends response (§8.20)."""
        response = client.get("/api/v1/dashboard/trends?days=7")
        assert response.status_code == 200
        body = response.json()
        assert "hitl_volume" in body
        assert "rejection_trend" in body
        assert "correlation" in body
        assert "feedback_volume" in body

    def test_hitl_volume_structure(self, client: TestClient) -> None:
        response = client.get("/api/v1/dashboard/trends?days=7")
        assert response.status_code == 200
        body = response.json()
        for entry in body["hitl_volume"]:
            assert "date" in entry
            assert "total_decisions" in entry
            assert "approved_count" in entry
            assert "rejected_count" in entry
            assert "rejection_rate" in entry
            assert "avg_time_to_approve_ms" in entry
            assert entry["avg_time_to_approve_ms"] is None or isinstance(entry["avg_time_to_approve_ms"], (int, float))

    def test_rejection_trend_structure(self, client: TestClient) -> None:
        response = client.get("/api/v1/dashboard/trends?days=7")
        assert response.status_code == 200
        body = response.json()
        for entry in body["rejection_trend"]:
            assert "date" in entry
            assert "rolling_rejection_rate" in entry
            assert "raw_rejection_rate" in entry

    def test_correlation_structure(self, client: TestClient) -> None:
        response = client.get("/api/v1/dashboard/trends?days=7")
        assert response.status_code == 200
        body = response.json()
        for entry in body["correlation"]:
            assert "date" in entry
            assert "rejection_rate" in entry
            assert "eval_pass_rate" in entry

    def test_feedback_volume_structure(self, client: TestClient) -> None:
        response = client.get("/api/v1/dashboard/trends?days=7")
        assert response.status_code == 200
        body = response.json()
        for entry in body["feedback_volume"]:
            assert "date" in entry
            assert "feedback_count" in entry
            assert "resolved_count" in entry
            assert "correcting_count" in entry

    def test_all_trends_align_by_day_count(self, client: TestClient) -> None:
        response = client.get("/api/v1/dashboard/trends?days=7")
        assert response.status_code == 200
        body = response.json()
        expected_len = 7
        assert len(body["hitl_volume"]) == expected_len
        assert len(body["rejection_trend"]) == expected_len
        assert len(body["correlation"]) == expected_len
        assert len(body["feedback_volume"]) == expected_len


class TestFactsWindowRunDate:
    """Period windows filter ``run_daily_facts`` on ``run_date`` (day-level
    bucket key), not ``created_at`` — backfilled rows carry a recent
    ``created_at`` but a ``run_date`` inside the target window (FAR-115)."""

    _FACT_ROW: ClassVar[dict[str, int | float]] = {
        "total": 5,
        "active_pipelines": 2,
        "tokens": 100,
        "avg_duration_ms": 150.0,
        "complete": 4,
    }

    def _compiled_sql(self, stmt: Any) -> str:
        return str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))

    async def test_facts_window_filters_on_run_date(self) -> None:
        start = date(2026, 1, 1)
        end = date(2026, 1, 8)
        session = _CapturingSession(_MockResult(rows=[_MockRow(**self._FACT_ROW)]))
        out = await _facts_window(session, _ORG_ID, start, end)
        assert out["total_runs"] == 5
        assert out["active_pipelines"] == 2
        assert out["success_rate"] == 80.0
        sql = self._compiled_sql(session.statements[0])
        assert "run_daily_facts" in sql
        assert "run_date" in sql
        assert "created_at" not in sql
        assert "2026-01-01" in sql
        assert "2026-01-08" in sql

    async def test_facts_status_counts_filters_on_run_date(self) -> None:
        start = date(2026, 1, 1)
        end = date(2026, 1, 8)
        session = _CapturingSession(
            _MockResult(rows=[_MockRow(status="complete", cnt=3), _MockRow(status="failed", cnt=1)])
        )
        out = await _facts_status_counts(session, _ORG_ID, start, end)
        assert out == {"complete": 3, "failed": 1}
        sql = self._compiled_sql(session.statements[0])
        assert "run_date" in sql
        assert "created_at" not in sql
        assert "2026-01-01" in sql
        assert "2026-01-08" in sql

    async def test_compute_period_metrics_uses_day_boundaries(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Facts/status/spend windows get ``[today - days, today)`` day-bucket
        boundaries; the eval window keeps now-based datetimes."""
        facts_calls: list[tuple[date, date]] = []
        status_calls: list[tuple[date, date]] = []
        spend_calls: list[tuple[date, date]] = []
        eval_calls: list[tuple[datetime, datetime]] = []

        async def _fake_facts(_session: Any, _org_id: Any, start: date, end: date) -> dict[str, Any]:
            facts_calls.append((start, end))
            return {"total_runs": 0, "active_pipelines": 0, "tokens": 0, "avg_duration_ms": None, "success_rate": None}

        async def _fake_status(_session: Any, _org_id: Any, start: date, end: date) -> dict[str, int]:
            status_calls.append((start, end))
            return {}

        async def _fake_spend(_session: Any, _org_id: Any, start: date, end: date) -> float:
            spend_calls.append((start, end))
            return 0.0

        async def _fake_eval(_session: Any, _org_id: Any, start: datetime, end: datetime) -> float | None:
            eval_calls.append((start, end))
            return None

        monkeypatch.setattr("modulo.api.routes.dashboard._facts_window", _fake_facts)
        monkeypatch.setattr("modulo.api.routes.dashboard._facts_status_counts", _fake_status)
        monkeypatch.setattr("modulo.api.routes.dashboard._ledger_spend_window", _fake_spend)
        monkeypatch.setattr("modulo.api.routes.dashboard._eval_rate_window", _fake_eval)

        await _compute_period_metrics(None, _ORG_ID, 7)  # type: ignore[arg-type]

        today = datetime.now(UTC).date()
        current_start = today - timedelta(days=7)
        prev_start = today - timedelta(days=14)
        prev_end = today - timedelta(days=7)

        assert facts_calls == [(current_start, today), (prev_start, prev_end)]
        assert status_calls == facts_calls
        assert spend_calls == facts_calls
        # Eval windows stay timestamp-based (evaluated_at is not a day bucket).
        assert all(isinstance(s, datetime) and isinstance(e, datetime) for s, e in eval_calls)
        assert len(eval_calls) == 2
        assert all((e - s) == timedelta(days=7) for s, e in eval_calls)
