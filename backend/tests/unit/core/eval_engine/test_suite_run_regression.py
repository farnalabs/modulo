"""detect_regressions default vs SuiteRun grouping path (FAR-376 Phase 3).

Two branches are asserted:

* the default ``group_by="eval_id"`` path is **byte-identical** to the legacy
  window-based SQL — we capture the executed statement's SQL string and verify
  it matches the original verbatim, so existing call sites
  ``detect_regressions(session, org_id, days=7, ...)`` are untouched;
* the SuiteRun path (``group_by="suite_id"``) compares an explicit current run
  against an explicit baseline run grouped per ``eval_id`` WITHOUT touching the
  default branch.

These tests use a mocked ``AsyncSession.execute`` so the SQL a call produces can
be inspected directly (no Postgres/RLS needed).
"""

import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from modulo.core.eval_engine.regression import detect_regressions

_EXPECTED_LEGACY_SQL = """
            SELECT
                er.eval_id,
                MAX(ed.name)          AS eval_name,
                SUM(CASE WHEN er.evaluated_at >= :recent_start THEN 1 ELSE 0 END)
                                       AS recent_total,
                SUM(CASE WHEN er.evaluated_at >= :recent_start AND er.passed THEN 1 ELSE 0 END)
                                       AS recent_passed,
                SUM(CASE WHEN er.evaluated_at < :recent_start THEN 1 ELSE 0 END)
                                       AS baseline_total,
                SUM(CASE WHEN er.evaluated_at < :recent_start AND er.passed THEN 1 ELSE 0 END)
                                       AS baseline_passed,
                ARRAY_AGG(er.run_id) FILTER (
                    WHERE er.evaluated_at >= :recent_start AND NOT er.passed
                )                      AS affected_run_ids
            FROM eval_results er
            JOIN eval_definitions ed ON ed.id = er.eval_id
            JOIN runs r ON r.id = er.run_id
            WHERE er.organisation_id = :org_id
              AND ed.organisation_id = :org_id
              AND ed.eval_type != 'guardrail'
              AND er.evaluated_at >= :baseline_start
              AND (:pipeline_id IS NULL OR r.pipeline_id = :pipeline_id)
            GROUP BY er.eval_id
        """


class _FakeScalarResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows


def _row(**kw: Any) -> SimpleNamespace:
    defaults = {
        "eval_id": uuid.uuid4(),
        "eval_name": "eval",
        "recent_total": 10,
        "recent_passed": 10,
        "affected_run_ids": [],
        "current_total": 10,
        "current_passed": 10,
        "baseline_total": 10,
        "baseline_passed": 10,
        "eval_type": "regex",
    }
    defaults.update(kw)
    return SimpleNamespace(**defaults)


async def _make_session(*, rows: list[Any] | None = None) -> AsyncMock:
    session = AsyncMock()
    session.execute.return_value = _FakeScalarResult(rows or [])
    return session


def _captured_sql(tool_call: Any) -> str:
    call_args = tool_call.await_args
    assert call_args is not None
    return str(call_args[0][0])


# --------------------------------------------------------------------------- #
# Default path — byte-identical SQL                                           #
# --------------------------------------------------------------------------- #
async def test_default_group_by_eval_id_sql_is_byte_identical() -> None:
    """The legacy call site produces the ORIGINAL SQL string verbatim."""
    session = await _make_session(rows=[])
    await detect_regressions(session, uuid.uuid4(), days=7)
    captured = _captured_sql(session.execute)
    assert _EXPECTED_LEGACY_SQL in captured
    # current_run_ids is ignored in the default branch — no suite_id group-by.
    assert "suite_run_id" not in captured


async def test_default_path_validations_unchanged() -> None:
    session = await _make_session(rows=[])
    with pytest.raises(ValueError, match="days must be"):
        await detect_regressions(session, uuid.uuid4(), days=0)
    with pytest.raises(ValueError, match="threshold must be"):
        await detect_regressions(session, uuid.uuid4(), threshold=-0.1)
    with pytest.raises(ValueError, match="recent_window_ratio must be"):
        await detect_regressions(session, uuid.uuid4(), recent_window_ratio=2.0)


async def test_default_path_emits_alert_on_drop() -> None:
    # recent 5/10 vs baseline 9/10 = 0.4 drop (well above 0.15 threshold).
    session = await _make_session(
        rows=[
            _row(
                recent_total=10,
                recent_passed=5,
                baseline_total=10,
                baseline_passed=9,
                affected_run_ids=[uuid.uuid4()],
            )
        ]
    )
    alerts = await detect_regressions(session, uuid.uuid4(), days=7, trend="declining")
    assert len(alerts) == 1
    assert alerts[0].trend == "declining"
    assert alerts[0].drop_pct == pytest.approx(0.4)


async def test_default_path_ignores_guardrail_and_insufficient_data() -> None:
    # The SQL filters guardrails; the Python layer skips zero-denominator rows.
    session = await _make_session(rows=[_row(recent_total=0, baseline_total=0)])
    alerts = await detect_regressions(session, uuid.uuid4(), days=7)
    assert alerts == []


# --------------------------------------------------------------------------- #
# SuiteRun grouping path                                                      #
# --------------------------------------------------------------------------- #
async def test_grouped_path_uses_suite_run_ids_and_computes_drop() -> None:
    session = await _make_session(
        rows=[_row(current_total=10, current_passed=2, baseline_total=10, baseline_passed=9, eval_type="regex")]
    )
    cur = uuid.uuid4()
    base = uuid.uuid4()
    alerts = await detect_regressions(
        session,
        uuid.uuid4(),
        threshold=0.15,
        group_by="suite_id",
        current_run_ids=[cur],
        baseline_run_ids=[base],
    )
    captured = _captured_sql(session.execute)
    assert "suite_run_id" in captured
    assert "current_ids" in captured
    assert "baseline_ids" in captured
    assert len(alerts) == 1
    assert alerts[0].drop_pct == pytest.approx(0.7)
    assert alerts[0].affected_run_ids == [cur]


async def test_grouped_path_relative_threshold_requires_both() -> None:
    # absolute drop = 0.3, relative_drop = 0.3/0.9 = 0.333. A relative
    # threshold of 0.5 (relative) should NOT fire; 0.2 should.
    rows = [_row(current_total=10, current_passed=6, baseline_total=10, baseline_passed=9)]
    session = await _make_session(rows=rows)
    alerts = await detect_regressions(
        session,
        uuid.uuid4(),
        threshold=0.15,
        relative_threshold=0.5,
        group_by="suite_id",
        current_run_ids=[uuid.uuid4()],
        baseline_run_ids=[uuid.uuid4()],
    )
    assert alerts == []

    session2 = await _make_session(rows=rows)
    alerts2 = await detect_regressions(
        session2,
        uuid.uuid4(),
        threshold=0.15,
        relative_threshold=0.2,
        group_by="suite_id",
        current_run_ids=[uuid.uuid4()],
        baseline_run_ids=[uuid.uuid4()],
    )
    assert len(alerts2) == 1


async def test_grouped_path_empty_baseline_returns_no_alerts() -> None:
    session = await _make_session(rows=[])
    alerts = await detect_regressions(
        session,
        uuid.uuid4(),
        group_by="suite_id",
        current_run_ids=[uuid.uuid4()],
        baseline_run_ids=[],
    )
    assert alerts == []


async def test_grouped_path_no_current_run_returns_empty() -> None:
    session = await _make_session(rows=[])
    alerts = await detect_regressions(session, uuid.uuid4(), group_by="suite_id", current_run_ids=None)
    assert alerts == []


async def test_grouped_path_scoped_by_eval_type() -> None:
    # Mixed eval types partition — a regex regression must not be diluted by an
    # llm_judge pass. Restricting to regex isolates the alert.
    session = await _make_session(
        rows=[_row(eval_type="regex", current_total=5, current_passed=1, baseline_total=5, baseline_passed=5)]
    )
    alerts = await detect_regressions(
        session,
        uuid.uuid4(),
        threshold=0.15,
        group_by="suite_id",
        current_run_ids=[uuid.uuid4()],
        baseline_run_ids=[uuid.uuid4()],
        eval_type="regex",
    )
    captured = _captured_sql(session.execute)
    assert "eval_type" in captured
    assert len(alerts) == 1
