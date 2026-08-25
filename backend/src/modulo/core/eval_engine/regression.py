"""Eval quality regression detection.

Compares pass rates between a recent window and a baseline window
for each eval definition, flagging significant drops.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal, get_args
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.eval_engine.okr import TrendDirection

_log = logging.getLogger(__name__)


VALID_TRENDS = frozenset(get_args(TrendDirection))


@dataclass
class RegressionAlert:
    """Alert for a single eval whose pass rate dropped significantly."""

    eval_id: UUID
    eval_name: str
    prev_pass_rate: float
    current_pass_rate: float
    drop_pct: float
    trend: str  # "declining" | "stable" | "improving"
    affected_run_ids: list[UUID] = field(default_factory=list)


async def detect_regressions(
    session: AsyncSession,
    org_id: UUID,
    days: int = 7,
    threshold: float = 0.15,
    recent_window_ratio: float = 0.25,
    pipeline_id: UUID | None = None,
    trend: str | None = None,
    *,
    group_by: Literal["eval_id", "suite_id"] = "eval_id",
    current_run_ids: Sequence[UUID] | None = None,
    baseline_run_ids: Sequence[UUID] | None = None,
    eval_type: str | None = None,
    relative_threshold: float | None = None,
) -> list[RegressionAlert]:
    """Detect pass-rate regressions by comparing recent vs baseline windows.

    The lookback period is split into a *baseline* window (earlier portion)
    and a *recent* window (last ``max(int(days * recent_window_ratio), 1)``
    days, default 25% of the lookback).  Alerts are emitted for evals whose
    pass rate dropped by at least *threshold*.

    Args:
        session: Async DB session.
        org_id: Organisation to scope the query.
        days: Total lookback period in days.
        threshold: Minimum absolute drop (as a fraction, e.g. ``0.15``)
            to trigger an alert.
        recent_window_ratio: Fraction of the lookback period used as the
            "recent" window (must be ``> 0`` and ``<= 1.0``).  Defaults to
            ``0.25`` (i.e. the legacy ``max(days // 4, 1)`` behaviour).
        pipeline_id: Optional pipeline to scope results to. When given, only
            eval results from runs of that pipeline are considered.
        trend: Optional trend filter — one of ``declining``, ``stable`` or
            ``improving``. When given, only alerts with that trend are
            returned (e.g. ``declining`` reduces noise to true regressions).
        group_by: Comparison axis. ``"eval_id"`` (the default) compares the
            recent window against a baseline window keyed by the lookback clock.
            ``"suite_id"`` compares an explicit current run against an explicit
            baseline run (the SuiteRun path) — see ``current_run_ids`` /
            ``baseline_run_ids``. The default value preserves the legacy
            byte-for-byte behaviour.
        current_run_ids: SuiteRun path only. The run(s) whose outcomes are the
            "current" pass rate. Ignored (and required to be None) in the
            default ``eval_id`` mode.
        baseline_run_ids: SuiteRun path only. The same-tuple run(s) whose
            outcomes are the baseline pass rate. When empty/None the comparison
            is skipped (no prior completed run).
        eval_type: SuiteRun path only. Restrict the comparison to a single
            ``eval_type`` so pass rates are never cross-aggregated across
            differing eval types.
        relative_threshold: SuiteRun path only. When set, an alert fires only
            when BOTH the absolute drop exceeds ``threshold`` AND the relative
            drop (``drop / prev_pass_rate``) exceeds ``relative_threshold``.

    Returns:
        List of ``RegressionAlert`` for evals with significant drops.

    """
    if days < 1:
        raise ValueError(f"days must be >= 1, got {days}")
    if threshold < 0:
        raise ValueError(f"threshold must be >= 0, got {threshold}")
    if not 0 < recent_window_ratio <= 1.0:
        raise ValueError(f"recent_window_ratio must be > 0 and <= 1.0, got {recent_window_ratio}")
    if trend is not None and trend not in VALID_TRENDS:
        raise ValueError(f"trend must be one of {sorted(VALID_TRENDS)}, got {trend!r}")
    if group_by not in ("eval_id", "suite_id"):
        raise ValueError(f"group_by must be 'eval_id' or 'suite_id', got {group_by!r}")

    # SuiteRun comparison path: explicit current vs baseline run comparison,
    # grouped per eval_id, scoped to the supplied run ids. Distinct from the
    # legacy clock-window path (below) which for example call sites must remain
    # byte-identical.
    if group_by == "suite_id":
        return await _detect_regressions_grouped(
            session, org_id, threshold, trend, current_run_ids, baseline_run_ids, eval_type, relative_threshold
        )

    now = datetime.now(UTC)
    recent_window_days = max(int(days * recent_window_ratio), 1)
    if recent_window_days >= days:
        recent_window_days = max(days // 2, 1)
    baseline_start = now - timedelta(days=days)
    recent_start = now - timedelta(days=recent_window_days)

    try:
        q = text("""
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
        """)

        rows = (
            await session.execute(
                q,
                {
                    "org_id": org_id,
                    "baseline_start": baseline_start,
                    "recent_start": recent_start,
                    "pipeline_id": pipeline_id,
                },
            )
        ).all()
    except TimeoutError:
        _log.error("Regression detection query timed out for org %s (days=%s)", org_id, days)
        raise
    except SQLAlchemyError:
        _log.exception("Regression detection DB error for org %s (days=%s)", org_id, days)
        raise

    alerts: list[RegressionAlert] = []
    for row in rows:
        recent_total: int = row.recent_total or 0
        recent_passed: int = row.recent_passed or 0
        baseline_total: int = row.baseline_total or 0
        baseline_passed: int = row.baseline_passed or 0

        if recent_total == 0 or baseline_total == 0:
            _log.info(
                "Skipping eval %s (%s) — insufficient data for regression check (recent=%s, baseline=%s)",
                row.eval_id,
                row.eval_name,
                recent_total,
                baseline_total,
            )
            continue

        current_pass_rate = recent_passed / recent_total
        prev_pass_rate = baseline_passed / baseline_total
        drop = prev_pass_rate - current_pass_rate

        if drop > threshold:
            trend_label = "declining"
        elif drop < -threshold:
            trend_label = "improving"
        else:
            trend_label = "stable"
        if trend is not None and trend_label != trend:
            continue
        alerts.append(
            RegressionAlert(
                eval_id=row.eval_id,
                eval_name=row.eval_name,
                prev_pass_rate=round(prev_pass_rate, 4),
                current_pass_rate=round(current_pass_rate, 4),
                drop_pct=round(drop, 4),
                trend=trend_label,
                affected_run_ids=list(row.affected_run_ids or []),
            ),
        )

    return alerts


async def _detect_regressions_grouped(
    session: AsyncSession,
    org_id: UUID,
    threshold: float,
    trend: str | None,
    current_run_ids: Sequence[UUID] | None,
    baseline_run_ids: Sequence[UUID] | None,
    eval_type: str | None,
    relative_threshold: float | None,
) -> list[RegressionAlert]:
    """Compare an explicit current run against a baseline run, grouped per eval.

    Both ``current_run_ids`` and ``baseline_run_ids`` reference rows in
    ``eval_results.suite_run_id``. Pass rates for the current and baseline runs
    are computed independently per ``eval_id``; a regression fires only when the
    absolute drop (and, when ``relative_threshold`` is set, the relative drop)
    exceeds the configured thresholds. Scoped strictly to ``org_id`` so a
    cross-org run can never be selected as a baseline.
    """
    current_ids = [str(r) for r in (current_run_ids or [])]
    baseline_ids = [str(r) for r in (baseline_run_ids or [])]
    if not current_ids:
        return []
    all_ids = current_ids + baseline_ids
    if not all_ids:
        return []

    try:
        q = text("""
            SELECT
                er.eval_id,
                MAX(ed.name) AS eval_name,
                ed.eval_type AS eval_type,
                SUM(CASE WHEN er.suite_run_id = ANY(:current_ids) THEN 1 ELSE 0 END)  AS current_total,
                SUM(
                    CASE WHEN er.suite_run_id = ANY(:current_ids) AND er.passed THEN 1 ELSE 0 END
                ) AS current_passed,
                SUM(CASE WHEN er.suite_run_id = ANY(:baseline_ids) THEN 1 ELSE 0 END) AS baseline_total,
                SUM(
                    CASE WHEN er.suite_run_id = ANY(:baseline_ids) AND er.passed THEN 1 ELSE 0 END
                ) AS baseline_passed
            FROM eval_results er
            JOIN eval_definitions ed ON ed.id = er.eval_id
            WHERE er.organisation_id = :org_id
              AND ed.organisation_id = :org_id
              AND ed.eval_type != 'guardrail'
              AND er.suite_run_id = ANY(:all_ids)
              AND (:eval_type IS NULL OR ed.eval_type = :eval_type)
            GROUP BY er.eval_id, ed.eval_type
        """)
        rows = (
            await session.execute(
                q,
                {
                    "org_id": org_id,
                    "current_ids": current_ids,
                    "baseline_ids": baseline_ids,
                    "all_ids": all_ids,
                    "eval_type": eval_type,
                },
            )
        ).all()
    except TimeoutError:
        _log.exception("Grouped regression detection query timed out for org %s", org_id)
        raise
    except SQLAlchemyError:
        _log.exception("Grouped regression detection DB error for org %s", org_id)
        raise

    alerts: list[RegressionAlert] = []
    for row in rows:
        current_total: int = row.current_total or 0
        current_passed: int = row.current_passed or 0
        baseline_total: int = row.baseline_total or 0
        baseline_passed: int = row.baseline_passed or 0
        if current_total == 0 or baseline_total == 0:
            continue

        current_pass_rate = current_passed / current_total
        prev_pass_rate = baseline_passed / baseline_total
        drop = prev_pass_rate - current_pass_rate

        exceeded = drop > threshold
        if exceeded and relative_threshold is not None:
            relative_drop = drop / prev_pass_rate if prev_pass_rate > 0 else 0.0
            exceeded = relative_drop > relative_threshold

        # The SuiteRun path is a *detector*: only a true regression (drop past
        # the thresholds) yields an alert, so ``bool(alerts)`` is a clean
        # regression flag. The default window path (above) intentionally keeps
        # its legacy behaviour of reporting the full trend readout.
        if not exceeded:
            continue
        if trend is not None and trend != "declining":
            continue
        alerts.append(
            RegressionAlert(
                eval_id=row.eval_id,
                eval_name=row.eval_name,
                prev_pass_rate=round(prev_pass_rate, 4),
                current_pass_rate=round(current_pass_rate, 4),
                drop_pct=round(drop, 4),
                trend="declining",
                affected_run_ids=[UUID(x) for x in current_ids],
            ),
        )

    return alerts
