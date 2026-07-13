"""Eval quality regression detection.

Compares pass rates between a recent window and a baseline window
for each eval definition, flagging significant drops.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

_log = logging.getLogger(__name__)


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
) -> list[RegressionAlert]:
    """Detect pass-rate regressions by comparing recent vs baseline windows.

    The lookback period is split into a *baseline* window (earlier portion)
    and a *recent* window (last ``max(days // 4, 1)`` days).  Alerts are
    emitted for evals whose pass rate dropped by at least *threshold*.

    Args:
        session: Async DB session.
        org_id: Organisation to scope the query.
        days: Total lookback period in days.
        threshold: Minimum absolute drop (as a fraction, e.g. ``0.15``)
            to trigger an alert.

    Returns:
        List of ``RegressionAlert`` for evals with significant drops.
    """
    if days < 1:
        raise ValueError(f"days must be >= 1, got {days}")
    if threshold < 0:
        raise ValueError(f"threshold must be >= 0, got {threshold}")

    now = datetime.now(UTC)
    recent_window_days = max(days // 4, 1)
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
            WHERE er.organisation_id = :org_id
              AND ed.organisation_id = :org_id
              AND er.evaluated_at >= :baseline_start
            GROUP BY er.eval_id
        """)

        rows = (
            await session.execute(
                q,
                {
                    "org_id": org_id,
                    "baseline_start": baseline_start,
                    "recent_start": recent_start,
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
            trend = "declining"
        elif drop < -threshold:
            trend = "improving"
        else:
            trend = "stable"
        alerts.append(
            RegressionAlert(
                eval_id=row.eval_id,
                eval_name=row.eval_name,
                prev_pass_rate=round(prev_pass_rate, 4),
                current_pass_rate=round(current_pass_rate, 4),
                drop_pct=round(drop, 4),
                trend=trend,
                affected_run_ids=list(row.affected_run_ids or []),
            ),
        )

    return alerts
