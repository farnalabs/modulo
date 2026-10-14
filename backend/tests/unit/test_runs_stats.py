"""Unit tests for the /runs/stats aggregation (``get_run_stats``).

Covers the two dialect paths:

* Postgres — duration percentiles computed in SQL via ``percentile_cont`` so the
  endpoint never loads the whole window into Python. NULL durations are excluded
  by the ``started_at``/``completed_at`` IS NOT NULL predicates; an empty
  percentile group yields ``None`` in the response instead of crashing.
* Generic backends (SQLite, MariaDB) — fall back to loading runs and computing
  percentiles in Python via ``_percentile``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.run import _get_dialect_name, _percentile, get_run_stats


def _make_session(dialect: str) -> AsyncMock:
    """AsyncSession mock whose ``get_bind()`` reports the given dialect."""
    session = AsyncMock(spec=AsyncSession)
    bind = MagicMock()
    bind.dialect.name = dialect

    async def _get_bind() -> MagicMock:
        return bind

    session.get_bind = _get_bind
    return session


def _postgres_execute(captured, *, day_rows, overall_row, failure_rows):
    """AsyncMock side-effect: capture the statements and return canned results.

    The three statements are identified by their compiled SQL:
    the day query (neither marker), the overall percentile query
    (``percentile_cont``), and the failure-reason query (``error_code``).
    """

    async def _execute(stmt, *args, **kwargs):
        captured.append(stmt)
        compiled = str(stmt.compile(dialect=postgresql.dialect()))
        result = MagicMock()
        if "percentile_cont" in compiled:
            result.one.return_value = overall_row
        elif "error_code" in compiled:
            result.all.return_value = failure_rows
        else:
            result.all.return_value = day_rows
        return result

    return _execute


def _run(
    *,
    status: str,
    created_at: datetime,
    completed_at: datetime | None,
    started_at: datetime | None,
    error_code: str | None = None,
    run_classification: dict | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        status=status,
        created_at=created_at,
        completed_at=completed_at,
        started_at=started_at,
        error_code=error_code,
        run_classification=run_classification,
    )


async def test_get_run_stats_postgres_uses_percentile_cont_sql() -> None:
    session = _make_session("postgresql")
    captured: list = []
    now = datetime.now(UTC)
    day_row = MagicMock(day=now.date(), run_count=2, success=1, failed=1, benign_count=0, avg_duration=100.0)
    overall_row = MagicMock(p50=100.0, p95=250.0, p99=500.0, avg_duration=150.0)
    session.execute = AsyncMock(
        side_effect=_postgres_execute(
            captured,
            day_rows=[day_row],
            overall_row=overall_row,
            failure_rows=[("boom", 3)],
        )
    )

    result = await get_run_stats(session, "30d")

    overall_stmt = captured[1]
    compiled = overall_stmt.compile(dialect=postgresql.dialect())
    compiled_sql = str(compiled)
    assert "percentile_cont" in compiled_sql
    assert "WITHIN GROUP" in compiled_sql
    # NULL durations are excluded by the terminal/timestamp predicates.
    assert "completed_at IS NOT NULL" in compiled_sql
    assert "started_at IS NOT NULL" in compiled_sql
    # The 0.5 / 0.95 / 0.99 quantiles are bound parameters, not literals.
    assert any("percentile_cont" in key for key in compiled.params)

    assert result["total_runs"] == 2
    assert result["success_rate"] == 0.5
    assert result["avg_duration_ms"] == 150
    assert result["p50_duration_ms"] == 100
    assert result["p95_duration_ms"] == 250
    assert result["p99_duration_ms"] == 500
    assert result["runs_by_day"] == [{"date": str(now.date()), "count": 2, "success": 1, "failed": 1}]
    assert result["failure_by_reason"] == [{"reason": "boom", "count": 3}]
    assert result["avg_duration_by_day"] == [{"date": str(now.date()), "avg_ms": 100}]


async def test_get_run_stats_postgres_empty_percentile_group_returns_none() -> None:
    """Runs exist but none are completed: percentiles are NULL -> None, no crash."""
    session = _make_session("postgresql")
    captured: list = []
    now = datetime.now(UTC)
    day_row = MagicMock(day=now.date(), run_count=1, success=0, failed=0, benign_count=0, avg_duration=None)
    overall_row = MagicMock(p50=None, p95=None, p99=None, avg_duration=None)
    session.execute = AsyncMock(
        side_effect=_postgres_execute(
            captured,
            day_rows=[day_row],
            overall_row=overall_row,
            failure_rows=[],
        )
    )

    result = await get_run_stats(session, "30d")

    assert result["total_runs"] == 1
    assert result["p50_duration_ms"] is None
    assert result["p95_duration_ms"] is None
    assert result["p99_duration_ms"] is None
    assert result["avg_duration_ms"] == 0
    assert not result["avg_duration_by_day"]


async def test_get_run_stats_postgres_single_row_returns_that_value() -> None:
    """A one-row percentile group returns exactly that row's duration."""
    session = _make_session("postgresql")
    captured: list = []
    now = datetime.now(UTC)
    day_row = MagicMock(day=now.date(), run_count=1, success=1, failed=0, benign_count=0, avg_duration=42.0)
    overall_row = MagicMock(p50=42.0, p95=42.0, p99=42.0, avg_duration=42.0)
    session.execute = AsyncMock(
        side_effect=_postgres_execute(
            captured,
            day_rows=[day_row],
            overall_row=overall_row,
            failure_rows=[],
        )
    )

    result = await get_run_stats(session, "30d")

    assert result["p50_duration_ms"] == 42
    assert result["p95_duration_ms"] == 42
    assert result["p99_duration_ms"] == 42


async def test_get_run_stats_postgres_empty_window_returns_zero_shape() -> None:
    """No runs at all: identical zero-shaped response to the generic path."""
    session = _make_session("postgresql")
    captured: list = []
    session.execute = AsyncMock(
        side_effect=_postgres_execute(
            captured,
            day_rows=[],
            overall_row=MagicMock(),
            failure_rows=[],
        )
    )

    result = await get_run_stats(session, "30d")

    assert result == {
        "total_runs": 0,
        "success_rate": 0.0,
        "avg_duration_ms": 0,
        "p50_duration_ms": 0,
        "p95_duration_ms": 0,
        "p99_duration_ms": 0,
        "runs_by_day": [],
        "failure_by_reason": [],
        "avg_duration_by_day": [],
    }


async def test_get_run_stats_generic_path_still_uses_percentile() -> None:
    """SQLite/MariaDB fall back to loading runs and calling ``_percentile``."""
    session = _make_session("sqlite")
    now = datetime.now(UTC)
    runs = [
        _run(
            status="complete",
            created_at=now,
            completed_at=now,
            started_at=now - timedelta(seconds=2),
        ),
        _run(
            status="failed",
            created_at=now - timedelta(days=1),
            completed_at=now - timedelta(days=1),
            started_at=now - timedelta(days=1, seconds=1),
            error_code="boom",
        ),
        _run(
            status="pending",
            created_at=now - timedelta(days=2),
            completed_at=None,
            started_at=None,
        ),
    ]
    res = MagicMock()
    res.scalars.return_value.all.return_value = runs
    session.execute = AsyncMock(return_value=res)

    with patch("modulo.db.crud.run._percentile", return_value=42.0) as pctl:
        result = await get_run_stats(session, "30d")

    assert pctl.call_count == 3
    assert result["total_runs"] == 3
    assert result["p50_duration_ms"] == 42
    assert result["p95_duration_ms"] == 42
    assert result["p99_duration_ms"] == 42
    assert result["avg_duration_ms"] == 1500
    assert result["failure_by_reason"] == [{"reason": "boom", "count": 1}]


async def test_get_run_stats_generic_empty_window_returns_zero_shape() -> None:
    session = _make_session("sqlite")
    res = MagicMock()
    res.scalars.return_value.all.return_value = []
    session.execute = AsyncMock(return_value=res)

    result = await get_run_stats(session, "30d")

    assert result["total_runs"] == 0
    assert result["p50_duration_ms"] == 0
    assert not result["runs_by_day"]


async def test_get_run_stats_generic_counts_cancelled_expired_eval_failed_as_failed() -> None:
    """Non-complete terminal statuses land in the per-day failed bucket."""
    session = _make_session("sqlite")
    now = datetime.now(UTC)
    runs = [
        _run(
            status="cancelled",
            created_at=now,
            completed_at=now - timedelta(minutes=1),
            started_at=now - timedelta(minutes=2),
        ),
        _run(
            status="expired",
            created_at=now - timedelta(days=1),
            completed_at=None,
            started_at=None,
        ),
        _run(
            status="eval_failed",
            created_at=now - timedelta(days=2),
            completed_at=None,
            started_at=None,
        ),
        _run(
            status="failed",
            created_at=now - timedelta(days=3),
            completed_at=None,
            started_at=None,
            error_code="boom",
        ),
        _run(
            status="stalled",
            created_at=now - timedelta(days=4),
            completed_at=None,
            started_at=None,
            error_code="executor_stalled",
        ),
    ]
    res = MagicMock()
    res.scalars.return_value.all.return_value = runs
    session.execute = AsyncMock(return_value=res)

    result = await get_run_stats(session, "30d")

    assert result["total_runs"] == 5
    assert result["success_rate"] == 0.0
    assert result["runs_by_day"] == [
        {"date": str((now - timedelta(days=4)).date()), "count": 1, "success": 0, "failed": 1},
        {"date": str((now - timedelta(days=3)).date()), "count": 1, "success": 0, "failed": 1},
        {"date": str((now - timedelta(days=2)).date()), "count": 1, "success": 0, "failed": 1},
        {"date": str((now - timedelta(days=1)).date()), "count": 1, "success": 0, "failed": 1},
        {"date": str(now.date()), "count": 1, "success": 0, "failed": 1},
    ]
    assert result["failure_by_reason"] == [{"reason": "boom", "count": 1}, {"reason": "executor_stalled", "count": 1}]


async def test_get_run_stats_generic_period_mapping() -> None:
    """7d/30d/90d map to the correct cutoffs; unknown periods fall back to 30d."""
    session = _make_session("sqlite")
    now = datetime.now(UTC)
    for period, days in [("7d", 7), ("30d", 30), ("90d", 90), ("bogus", 30)]:
        captured_cutoff: list[datetime] = []

        async def _fake_python(_session, cutoff, *, _captured=captured_cutoff):
            _captured.append(cutoff)
            return _empty_stats()

        with patch("modulo.db.crud.run._get_run_stats_python", side_effect=_fake_python):
            await get_run_stats(session, period)

        expected_cutoff = now - timedelta(days=days)
        assert len(captured_cutoff) == 1, f"period {period}"
        assert abs((captured_cutoff[0] - expected_cutoff).total_seconds()) < 60, f"period {period}"


def _empty_stats() -> dict:
    return {
        "total_runs": 0,
        "success_rate": 0.0,
        "avg_duration_ms": 0,
        "p50_duration_ms": 0,
        "p95_duration_ms": 0,
        "p99_duration_ms": 0,
        "runs_by_day": [],
        "failure_by_reason": [],
        "avg_duration_by_day": [],
    }


async def test_get_run_stats_postgres_period_mapping() -> None:
    """Postgres fast path receives the same cutoff mapping as the generic path."""
    session = _make_session("postgresql")
    now = datetime.now(UTC)
    for period, days in [("7d", 7), ("30d", 30), ("90d", 90), ("bogus", 30)]:
        captured_cutoff: list[datetime] = []

        async def _fake_postgres(_session, cutoff, *, _captured=captured_cutoff):
            _captured.append(cutoff)
            return _empty_stats()

        with patch("modulo.db.crud.run._get_run_stats_postgres", side_effect=_fake_postgres):
            await get_run_stats(session, period)

        expected_cutoff = now - timedelta(days=days)
        assert len(captured_cutoff) == 1, f"period {period}"
        assert abs((captured_cutoff[0] - expected_cutoff).total_seconds()) < 60, f"period {period}"


def test_percentile_empty_returns_zero() -> None:
    assert _percentile([], 50) == 0.0


def test_percentile_single_value_returns_that_value() -> None:
    assert _percentile([42.0], 50) == 42.0


def test_percentile_interpolates_between_points() -> None:
    # Linear interpolation: p50 of [10, 20, 30, 40] is 25.0.
    assert _percentile([10.0, 20.0, 30.0, 40.0], 50) == 25.0
    assert _percentile([10.0, 20.0, 30.0, 40.0], 0) == 10.0
    assert _percentile([10.0, 20.0, 30.0, 40.0], 100) == 40.0


def test_percentile_clamps_last_bucket_to_final_value() -> None:
    # p95 of a two-element list interpolates between both elements.
    assert _percentile([100.0, 200.0], 95) == 195.0


async def test_get_dialect_name_awaits_coroutine_bind() -> None:
    session = _make_session("postgresql")
    assert await _get_dialect_name(session) == "postgresql"


async def test_get_dialect_name_handles_sync_bind() -> None:
    bind = MagicMock()
    bind.dialect.name = "sqlite"
    session = AsyncMock(spec=AsyncSession)
    session.get_bind.return_value = bind

    assert await _get_dialect_name(session) == "sqlite"


# ---------------------------------------------------------------------------
# FAR-707 — benign (business-expected) outcomes leave every failure rate
# ---------------------------------------------------------------------------


def _benign_classification() -> dict:
    return {"value": "benign", "reason": "benign_outcome"}


def _no_delivery_classification() -> dict:
    return {"value": "no_delivery", "reason": "source_error"}


async def test_get_run_stats_generic_drops_benign_from_the_failure_rate() -> None:
    """1 complete + 1 REAL failure + 1 benign rebase conflict.

    Pre-FAR-707 the rate was 1/3; the benign run leaves the DENOMINATOR too,
    so the rate is 1/2 and the benign run appears in neither the failed bucket
    nor the ``agent.failed`` failure-reason cluster. ``total_runs`` stays the
    raw volume (it counts runs, it is not a rate).
    """
    session = _make_session("sqlite")
    now = datetime.now(UTC)
    runs = [
        _run(
            status="complete",
            created_at=now,
            completed_at=now,
            started_at=now - timedelta(minutes=1),
        ),
        _run(
            status="failed",
            created_at=now,
            completed_at=now,
            started_at=now - timedelta(minutes=1),
            error_code="agent.failed",
            run_classification=_benign_classification(),
        ),
        _run(
            status="failed",
            created_at=now,
            completed_at=now,
            started_at=now - timedelta(minutes=1),
            error_code="node_cancelled",
            run_classification=_no_delivery_classification(),
        ),
    ]
    res = MagicMock()
    res.scalars.return_value.all.return_value = runs
    session.execute = AsyncMock(return_value=res)

    result = await get_run_stats(session, "30d")

    assert result["total_runs"] == 3
    assert result["success_rate"] == 0.5
    assert result["runs_by_day"] == [{"date": str(now.date()), "count": 3, "success": 1, "failed": 1}]
    assert result["failure_by_reason"] == [{"reason": "node_cancelled", "count": 1}]


async def test_get_run_stats_generic_keeps_an_unclassified_run_counting() -> None:
    """A terminal run whose classification has not landed yet (NULL record) is
    NOT benign — fail-open to the historical counting."""
    session = _make_session("sqlite")
    now = datetime.now(UTC)
    runs = [
        _run(
            status="failed",
            created_at=now,
            completed_at=now,
            started_at=now - timedelta(minutes=1),
            error_code="agent.failed",
            run_classification=None,
        ),
    ]
    res = MagicMock()
    res.scalars.return_value.all.return_value = runs
    session.execute = AsyncMock(return_value=res)

    result = await get_run_stats(session, "30d")

    assert result["success_rate"] == 0.0
    assert result["failure_by_reason"] == [{"reason": "agent.failed", "count": 1}]


async def test_get_run_stats_postgres_excludes_benign_from_failure_aggregates() -> None:
    """The Postgres day query AND the failure-reason query carry the benign
    exclusion, and the rate's denominator drops the per-day benign total."""
    session = _make_session("postgresql")
    captured: list = []
    now = datetime.now(UTC)
    day_row = MagicMock(day=now.date(), run_count=3, success=1, failed=1, benign_count=1, avg_duration=100.0)
    overall_row = MagicMock(p50=100.0, p95=250.0, p99=500.0, avg_duration=150.0)
    session.execute = AsyncMock(
        side_effect=_postgres_execute(
            captured,
            day_rows=[day_row],
            overall_row=overall_row,
            failure_rows=[("node_cancelled", 1)],
        )
    )

    result = await get_run_stats(session, "30d")

    day_sql = str(captured[0].compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    failure_sql = str(captured[2].compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    # ``as_string()`` renders a CAST around the ``->>`` extraction; pin the
    # WHOLE predicate so a silent swap to a plain ``!=`` (which drops NULL
    # classifications from the aggregate) fails the test.
    benign_predicate = "CAST((runs.run_classification ->> 'value') AS VARCHAR) IS DISTINCT FROM 'benign'"
    # The failed bucket skips benign rows...
    assert benign_predicate in day_sql
    # ...and the error-code cluster breakdown does too.
    assert benign_predicate in failure_sql
    # The per-day benign total is selected so the denominator is recomputable.
    assert "AS benign_count" in day_sql

    assert result["total_runs"] == 3
    # 1 complete over (3 - 1 benign) = 0.5 — the benign run leaves the rate.
    assert result["success_rate"] == 0.5
    assert result["runs_by_day"] == [{"date": str(now.date()), "count": 3, "success": 1, "failed": 1}]
