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

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.run import get_run_stats


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
) -> SimpleNamespace:
    return SimpleNamespace(
        status=status,
        created_at=created_at,
        completed_at=completed_at,
        started_at=started_at,
        error_code=error_code,
    )


def test_get_run_stats_postgres_uses_percentile_cont_sql() -> None:
    session = _make_session("postgresql")
    captured: list = []
    now = datetime.now(UTC)
    day_row = MagicMock(day=now.date(), run_count=2, success=1, failed=1, avg_duration=100.0)
    overall_row = MagicMock(p50=100.0, p95=250.0, p99=500.0, avg_duration=150.0)
    session.execute = AsyncMock(
        side_effect=_postgres_execute(
            captured,
            day_rows=[day_row],
            overall_row=overall_row,
            failure_rows=[("boom", 3)],
        )
    )

    result = asyncio.run(get_run_stats(session, "30d"))

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


def test_get_run_stats_postgres_empty_percentile_group_returns_none() -> None:
    """Runs exist but none are completed: percentiles are NULL -> None, no crash."""
    session = _make_session("postgresql")
    captured: list = []
    now = datetime.now(UTC)
    day_row = MagicMock(day=now.date(), run_count=1, success=0, failed=0, avg_duration=None)
    overall_row = MagicMock(p50=None, p95=None, p99=None, avg_duration=None)
    session.execute = AsyncMock(
        side_effect=_postgres_execute(
            captured,
            day_rows=[day_row],
            overall_row=overall_row,
            failure_rows=[],
        )
    )

    result = asyncio.run(get_run_stats(session, "30d"))

    assert result["total_runs"] == 1
    assert result["p50_duration_ms"] is None
    assert result["p95_duration_ms"] is None
    assert result["p99_duration_ms"] is None
    assert result["avg_duration_ms"] == 0
    assert result["avg_duration_by_day"] == []


def test_get_run_stats_postgres_single_row_returns_that_value() -> None:
    """A one-row percentile group returns exactly that row's duration."""
    session = _make_session("postgresql")
    captured: list = []
    now = datetime.now(UTC)
    day_row = MagicMock(day=now.date(), run_count=1, success=1, failed=0, avg_duration=42.0)
    overall_row = MagicMock(p50=42.0, p95=42.0, p99=42.0, avg_duration=42.0)
    session.execute = AsyncMock(
        side_effect=_postgres_execute(
            captured,
            day_rows=[day_row],
            overall_row=overall_row,
            failure_rows=[],
        )
    )

    result = asyncio.run(get_run_stats(session, "30d"))

    assert result["p50_duration_ms"] == 42
    assert result["p95_duration_ms"] == 42
    assert result["p99_duration_ms"] == 42


def test_get_run_stats_postgres_empty_window_returns_zero_shape() -> None:
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

    result = asyncio.run(get_run_stats(session, "30d"))

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


def test_get_run_stats_generic_path_still_uses_percentile() -> None:
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
        result = asyncio.run(get_run_stats(session, "30d"))

    assert pctl.call_count == 3
    assert result["total_runs"] == 3
    assert result["p50_duration_ms"] == 42
    assert result["p95_duration_ms"] == 42
    assert result["p99_duration_ms"] == 42
    assert result["avg_duration_ms"] == 1500
    assert result["failure_by_reason"] == [{"reason": "boom", "count": 1}]


def test_get_run_stats_generic_empty_window_returns_zero_shape() -> None:
    session = _make_session("sqlite")
    res = MagicMock()
    res.scalars.return_value.all.return_value = []
    session.execute = AsyncMock(return_value=res)

    result = asyncio.run(get_run_stats(session, "30d"))

    assert result["total_runs"] == 0
    assert result["p50_duration_ms"] == 0
    assert result["runs_by_day"] == []
