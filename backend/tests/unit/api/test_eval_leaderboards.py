"""Unit tests for the eval leaderboard / timeseries read-model (FAR-378).

Two layers are exercised separately:

* the PURE aggregation + query builders over mocked result rows — this is where
  the "pass-rate only, never raw score, mixed-eval_type partition" discipline is
  asserted, along with the org-scoped predicate (the ONLY isolation control);
* the API endpoints (`/api/v1/evals/leaderboard` and
  `/api/v1/evals/{eval_id}/timeseries`) via the TestClient + a mocked session,
  asserting the response shape, auth, and the 404-on-missing-eval path.

No real database is used — aggregation queries are inspected as SQL strings and
the Python roll-up is tested against hand-built row shapes, exactly like the
existing ``test_evals_dashboard.py`` and ``test_suite_run_regression.py``.
"""

import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.core.eval_engine.suite_run import (
    aggregate_eval_leaderboard,
    bucket_eval_timeseries,
    build_eval_leaderboard_query,
    build_eval_pipelines_query,
    build_eval_timeseries_query,
    summarise_eval_timeseries,
)
from modulo.settings import Settings, get_settings
from tests.unit.api.mock_session import configure_mock_session


@pytest.fixture(autouse=True)
def _clear_overrides() -> Generator[None, None, None]:
    """Clear FastAPI dependency overrides after every test (no cross-test leak).

    ``_client`` sets the overrides immediately before the request, so they must
    stay active for the whole test body and be cleared only in teardown.
    """
    yield
    app.dependency_overrides.clear()


_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_ORG_ID_2 = uuid.UUID("00000000-0000-0000-0000-000000000002")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")
_PIPELINE_1 = uuid.UUID("00000000-0000-0000-0000-000000000010")
_PIPELINE_2 = uuid.UUID("00000000-0000-0000-0000-000000000011")
_NODE_1 = uuid.UUID("00000000-0000-0000-0000-000000000020")
_EVAL_1 = uuid.UUID("00000000-0000-0000-0000-000000000030")
_EVAL_2 = uuid.UUID("00000000-0000-0000-0000-000000000031")


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #
def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


def _make_result(scalar=None, scalar_one_or_none=None, all_rows=None) -> MagicMock:
    """A result double with every consumer method configured (order-independent)."""
    m = MagicMock()
    m.scalar = MagicMock(return_value=scalar)
    m.scalar_one_or_none = MagicMock(return_value=scalar_one_or_none)
    m.all = MagicMock(return_value=all_rows if all_rows is not None else [])
    return m


def _make_mock_session() -> AsyncMock:
    session = AsyncMock()
    configure_mock_session(session)
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    session.execute = AsyncMock()
    bind_mock = MagicMock()
    bind_mock.dialect.name = "postgresql"
    session.get_bind = MagicMock(return_value=bind_mock)
    return session


def _lb_row(*, key: Any, label: Any = None, eval_type="regex", passed=0, total=0, run_count=0) -> SimpleNamespace:
    """Build a mock-leaderboard row (the SELECT row shape from the aggregation SQL)."""
    return SimpleNamespace(
        axis_key=key,
        axis_label=label if label is not None else key,
        eval_type=eval_type,
        passed_count=passed,
        total_count=total,
        run_count=run_count,
    )


def _ts_row(*, bucket: Any, eval_type="regex", passed=0, total=0, run_count=0) -> SimpleNamespace:
    """Build a mock-timeseries row (the SELECT row shape from the timeseries SQL)."""
    return SimpleNamespace(
        bucket=bucket, eval_type=eval_type, passed_count=passed, total_count=total, run_count=run_count
    )


def _pip_row(pipeline_id: uuid.UUID, name: str | None) -> SimpleNamespace:
    return SimpleNamespace(pipeline_id=pipeline_id, pipeline_name=name)


def _install_execute(
    session: AsyncMock,
    *,
    leaderboard_rows: list | None = None,
    ts_rows: list | None = None,
    pipeline_rows: list | None = None,
    eval_def: Any | None = None,
) -> AsyncMock:
    """Install a statement-aware ``execute`` so RLS/permission reads never starve.

    The route performs several session calls the test does not need to count
    precisely (RLS ``set_config`` calls, the ``require_permission`` authz
    kill-switch read, the eval-def lookup). Routing on the statement text keeps
    the mock robust to those ordering/count details and lets each test inject
    only the rows its query returns.
    """

    def _execute(stmt: Any, params: Any = None) -> Any:
        s = str(stmt)
        if "set_config" in s:
            return _make_result(scalar=None)
        if "authz_enforce" in s:
            return _make_result(scalar_one_or_none=None)
        if "DATE_TRUNC('day'" in s and "eval_type" in s:
            return _make_result(all_rows=ts_rows or [])
        if "pipeline_name" in s and "GROUP BY ed.pipeline_id" in s:
            return _make_result(all_rows=pipeline_rows or [])
        if "COUNT(*) FILTER" in s:
            return _make_result(all_rows=leaderboard_rows or [])
        if "eval_definitions" in s:
            return _make_result(scalar_one_or_none=eval_def)
        return _make_result(all_rows=[])

    session.execute.side_effect = _execute
    return session


# --------------------------------------------------------------------------- #
# Pure aggregation — pass-rate only, never raw score, mixed-type partition     #
# --------------------------------------------------------------------------- #
class TestAggregateLeaderboard:
    def test_mixed_eval_type_partition_never_ranks_by_score(self) -> None:
        """A mixed eval_type axis must partition and rank on pass-rate (booleans).

        The trap the read-model must avoid: ranking by raw ``score``. Here the
        ``llm_judge`` wins on any raw-score comparison but has ZERO passes while
        the ``regex`` entries all pass — a score-based ranking would be wrong.
        The partition + pass/total counting must rank ``Pip B`` (0.9) above
        ``Pip A`` (0.5) and must NEVER surface a ``score`` key.
        """
        rows = [
            # Pip A llm_judge: high raw score oracle but 0/10 pass.
            _lb_row(key=_PIPELINE_1, label="Pip A", eval_type="llm_judge", passed=0, total=10, run_count=2),
            # Pip A regex: 10/10.
            _lb_row(key=_PIPELINE_1, label="Pip A", eval_type="regex", passed=10, total=10, run_count=3),
            # Pip B llm_judge: 9/10.
            _lb_row(key=_PIPELINE_2, label="Pip B", eval_type="llm_judge", passed=9, total=10, run_count=1),
        ]
        entries = aggregate_eval_leaderboard(rows, group_by="pipeline")

        # Ranked by aggregate pass-rate (boolean passes), descending.
        assert entries[0]["key"] == str(_PIPELINE_2)
        assert entries[0]["pass_rate"] == pytest.approx(0.9)
        assert entries[1]["key"] == str(_PIPELINE_1)
        assert entries[1]["pass_rate"] == pytest.approx(0.5)

        # The type partition is preserved per axis — llm_judge is never diluted
        # by regex passes and never merged into a single raw score.
        pip_a = entries[1]
        assert pip_a["by_type"]["llm_judge"]["pass_rate"] == 0.0
        assert pip_a["by_type"]["regex"]["pass_rate"] == 1.0
        assert pip_a["by_type"]["llm_judge"]["total"] == 10
        assert pip_a["by_type"]["regex"]["total"] == 10

        # Raw score must never leak into the read-model.
        jack = _serialise(entries)
        assert "score" not in jack

    def test_rollup_counts_passes_across_types_not_scores(self) -> None:
        """The axis rollup is passed/total across partitions, not a score mean."""
        rows = [
            _lb_row(key=_PIPELINE_1, label="Pip A", eval_type="llm_judge", passed=8, total=20, run_count=1),
            _lb_row(key=_PIPELINE_1, label="Pip A", eval_type="regex", passed=2, total=5, run_count=1),
        ]
        entries = aggregate_eval_leaderboard(rows, group_by="pipeline")
        assert entries[0]["passed"] == 10
        assert entries[0]["total"] == 25
        assert entries[0]["pass_rate"] == pytest.approx(0.4)

    def test_stability_optimal_for_single_type(self) -> None:
        entries = aggregate_eval_leaderboard(
            [_lb_row(key=_PIPELINE_1, label="Pip A", eval_type="regex", passed=8, total=10)],
            group_by="pipeline",
        )
        assert entries[0]["stability"] == 1.0

    def test_null_axis_key_is_dropped(self) -> None:
        entries = aggregate_eval_leaderboard(
            [
                _lb_row(key=None, label=None, eval_type="regex", passed=1, total=1),
                _lb_row(key=_PIPELINE_1, label="Pip A", eval_type="regex", passed=1, total=1),
            ],
            group_by="pipeline",
        )
        assert len(entries) == 1
        assert entries[0]["key"] == str(_PIPELINE_1)

    def test_no_data_entry_sinks_to_bottom_with_null_pass_rate(self) -> None:
        entries = aggregate_eval_leaderboard(
            [
                _lb_row(key=_PIPELINE_1, label="Pip A", eval_type="regex", passed=9, total=10),
                _lb_row(key=_PIPELINE_2, label="Pip B", eval_type="regex", passed=0, total=0),
            ],
            group_by="pipeline",
        )
        assert entries[0]["key"] == str(_PIPELINE_1)
        assert entries[-1]["key"] == str(_PIPELINE_2)
        assert entries[-1]["pass_rate"] is None

    def test_invalid_axis_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="group_by"):
            aggregate_eval_leaderboard([], group_by="bogus")


def _serialise(obj: Any) -> Any:
    """JSON-round-trip a result to catch non-serialisable/misnamed keys."""
    import json

    return json.loads(json.dumps(obj, default=str))


# --------------------------------------------------------------------------- #
# Query builders — org-scoped predicate, pass-rate-only aggregation            #
# --------------------------------------------------------------------------- #
class TestBuildLeaderboardQuery:
    def test_query_is_org_scoped_and_pass_rate_only(self) -> None:
        statement, params = build_eval_leaderboard_query(org_id=_ORG_ID, group_by="pipeline")
        assert "er.organisation_id = :org_id" in statement
        assert "ed.organisation_id = :org_id" in statement
        assert "ed.eval_type != 'guardrail'" in statement
        assert "COUNT(*) FILTER (WHERE er.passed)" in statement
        # raw score is never aggregated — the oracle score column must not appear.
        assert "er.score" not in statement
        assert params["org_id"] == _ORG_ID
        assert "since" in params

    def test_query_mixed_type_partition_grouped_by_axis_and_eval_type(self) -> None:
        statement, _ = build_eval_leaderboard_query(org_id=_ORG_ID, group_by="pipeline")
        assert "GROUP BY ed.pipeline_id, ed.eval_type" in statement

    def test_query_agent_axis_requires_suite_run_and_joins_model_backend(self) -> None:
        statement, _ = build_eval_leaderboard_query(org_id=_ORG_ID, group_by="agent")
        assert "sr.model_backend_id" in statement
        assert "sr.model_backend_id IS NOT NULL" in statement
        assert "mb.name" in statement

    def test_query_node_axis_filters_null_node(self) -> None:
        statement, _ = build_eval_leaderboard_query(org_id=_ORG_ID, group_by="node")
        assert "ed.node_id IS NOT NULL" in statement

    def test_query_binds_filters_when_provided(self) -> None:
        statement, params = build_eval_leaderboard_query(
            org_id=_ORG_ID,
            group_by="pipeline",
            eval_id=_EVAL_1,
            pipeline_id=_PIPELINE_1,
            node_id=_NODE_1,
        )
        assert "er.eval_id = :eval_id" in statement
        assert "ed.pipeline_id = :pipeline_id" in statement
        assert "ed.node_id = :node_id" in statement
        assert params["eval_id"] == _EVAL_1
        assert params["pipeline_id"] == _PIPELINE_1
        assert params["node_id"] == _NODE_1

    def test_invalid_axis_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="group_by"):
            build_eval_leaderboard_query(org_id=_ORG_ID, group_by="bogus")


class TestBuildTimeseriesQuery:
    def test_timeseries_buckets_by_day_and_partitions_by_eval_type(self) -> None:
        statement, params = build_eval_timeseries_query(org_id=_ORG_ID, eval_id=_EVAL_1)
        assert "DATE_TRUNC('day', er.evaluated_at)" in statement
        assert "GROUP BY bucket, ed.eval_type" in statement
        assert "er.eval_id = :eval_id" in statement
        assert "COUNT(*) FILTER (WHERE er.passed)" in statement
        assert params["eval_id"] == _EVAL_1
        assert params["org_id"] == _ORG_ID

    def test_cross_org_predicate_always_present_in_pipeline_rollup(self) -> None:
        statement, params = build_eval_pipelines_query(org_id=_ORG_ID_2, eval_id=_EVAL_1)
        assert "er.organisation_id = :org_id" in statement
        assert params["org_id"] == _ORG_ID_2


# --------------------------------------------------------------------------- #
# Bucketing — zero-fill, absent day is null not 0.0                            #
# --------------------------------------------------------------------------- #
class TestBucketTimeseries:
    def test_zero_fills_grid_and_absent_day_is_null_pass_rate(self) -> None:
        since = datetime.now(UTC) - timedelta(days=2)
        day_mid = (since + timedelta(days=1)).date()
        rows = [_ts_row(bucket=day_mid, passed=9, total=10, run_count=3)]
        buckets = bucket_eval_timeseries(rows, since=since)
        assert len(buckets) >= 3  # zero-filled to today
        # the populated day carries its pass-rate.
        populated = [b for b in buckets if b["total"] > 0]
        assert any(b["pass_rate"] is not None and b["total"] == 10 for b in populated)
        # absent days are total=0 / pass_rate=None, never 0.0.
        for b in buckets:
            if b["total"] == 0:
                assert b["pass_rate"] is None
                assert b["passed"] == 0

    def test_summary_aggregates_without_zero_inflation(self) -> None:
        buckets = [
            {"date": "2026-01-01", "passed": 4, "total": 5, "pass_rate": 0.8, "run_count": 1},
            {"date": "2026-01-02", "passed": 0, "total": 0, "pass_rate": None, "run_count": 0},
            {"date": "2026-01-03", "passed": 1, "total": 5, "pass_rate": 0.2, "run_count": 1},
        ]
        summary = summarise_eval_timeseries(buckets)
        assert summary["passed"] == 5
        assert summary["total"] == 10
        assert summary["pass_rate"] == pytest.approx(0.5)
        assert summary["run_count"] == 2


# --------------------------------------------------------------------------- #
# API — /api/v1/evals/leaderboard                                             #
# --------------------------------------------------------------------------- #
class TestLeaderboardEndpoint:
    URL = "/api/v1/evals/leaderboard"

    def _client(self, mock_session: AsyncMock, *, org_role: str = "admin") -> TestClient:
        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield mock_session

        app.dependency_overrides[get_settings] = _make_settings
        app.dependency_overrides[get_db_session] = override_session
        app.dependency_overrides[_get_engine] = lambda: MagicMock()
        app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
            username="admin",
            organisation_id=_ORG_ID,
            account_id=_USER_ID,
            org_role=org_role,
        )
        mock_plan = MagicMock()
        mock_plan.feature_enabled.return_value = True
        app.dependency_overrides[get_plan_context] = lambda: mock_plan
        return TestClient(app)

    def test_leaderboard_returns_ranked_entries(self) -> None:
        session = _make_mock_session()
        rows = [
            _lb_row(key=_PIPELINE_1, label="Pip A", eval_type="regex", passed=10, total=10, run_count=2),
            _lb_row(key=_PIPELINE_2, label="Pip B", eval_type="regex", passed=5, total=10, run_count=1),
        ]
        _install_execute(session, leaderboard_rows=rows)
        client = self._client(session)
        resp = client.get(self.URL + "?group_by=pipeline")
        assert resp.status_code == 200
        data = resp.json()
        assert data["group_by"] == "pipeline"
        assert data["days"] == 30
        assert [e["key"] for e in data["entries"]] == [str(_PIPELINE_1), str(_PIPELINE_2)]
        assert data["entries"][0]["pass_rate"] == pytest.approx(1.0)

    def test_invalid_group_by_returns_422(self) -> None:
        session = _make_mock_session()
        _install_execute(session)
        client = self._client(session)
        resp = client.get(self.URL + "?group_by=bogus")
        assert resp.status_code == 422

    def test_unauthenticated_returns_401(self) -> None:
        app.dependency_overrides[get_settings] = _make_settings
        try:
            resp = TestClient(app).get(self.URL)
            assert resp.status_code in (401, 403)
        finally:
            app.dependency_overrides.clear()


# --------------------------------------------------------------------------- #
# API — /api/v1/evals/{eval_id}/timeseries                                    #
# --------------------------------------------------------------------------- #
class TestTimeseriesEndpoint:
    URL = "/api/v1/evals/{}/timeseries"

    def _client(self, mock_session: AsyncMock) -> TestClient:
        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield mock_session

        app.dependency_overrides[get_settings] = _make_settings
        app.dependency_overrides[get_db_session] = override_session
        app.dependency_overrides[_get_engine] = lambda: MagicMock()
        app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
            username="admin",
            organisation_id=_ORG_ID,
            account_id=_USER_ID,
            org_role="admin",
        )
        mock_plan = MagicMock()
        mock_plan.feature_enabled.return_value = True
        app.dependency_overrides[get_plan_context] = lambda: mock_plan
        return TestClient(app)

    def test_timeseries_returns_buckets_summary_and_pipelines(self) -> None:
        session = _make_mock_session()
        today = datetime.now(UTC).date()
        ts_rows = [_ts_row(bucket=today, passed=5, total=8, run_count=2)]
        pipeline_rows = [_pip_row(_PIPELINE_1, "Data Pipeline")]
        _install_execute(
            session,
            ts_rows=ts_rows,
            pipeline_rows=pipeline_rows,
            eval_def=SimpleNamespace(name="accuracy", id=_EVAL_1),
        )
        client = self._client(session)
        resp = client.get(self.URL.format(_EVAL_1))
        assert resp.status_code == 200
        data = resp.json()
        assert data["eval_id"] == str(_EVAL_1)
        assert data["eval_name"] == "accuracy"
        assert data["buckets"][-1]["total"] == 8
        assert data["buckets"][-1]["pass_rate"] == pytest.approx(5 / 8)
        assert data["summary"]["passed"] == 5
        assert data["summary"]["total"] == 8
        assert data["pipelines"] == [{"pipeline_id": str(_PIPELINE_1), "pipeline_name": "Data Pipeline"}]

    def test_timeseries_404_when_eval_not_found(self) -> None:
        session = _make_mock_session()
        _install_execute(session, eval_def=None)
        client = self._client(session)
        resp = client.get(self.URL.format(_EVAL_2))
        assert resp.status_code == 404
