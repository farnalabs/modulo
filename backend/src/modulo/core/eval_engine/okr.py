"""OKR-aligned eval suite progress tracking.

Tracks pass rate trends for eval suites against configurable thresholds,
providing breach detection and trend analysis for OKR alignment.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.util import sanitise_log_value as _sanitise_log_value

_log = logging.getLogger(__name__)

TrendDirection = Literal["declining", "stable", "improving"]


class OkrTrendPoint(BaseModel):
    """Pass rate for a single lookback period."""

    period: str  # "7d", "14d", "30d", "overall"
    pass_rate: float
    total_evals: int
    passed_evals: int


class OkrProgress(BaseModel):
    """Full OKR progress snapshot for a suite."""

    suite_id: str
    suite_name: str
    current_score: float
    pass_threshold: float | None
    trend: list[OkrTrendPoint]
    trend_direction: TrendDirection
    days_to_target: int | None
    breach: bool


def _rate(total: int, passed: int) -> float:
    return round(passed / total, 4) if total > 0 else 0.0


def _trend_point(period: str, total: int, passed: int) -> OkrTrendPoint:
    return OkrTrendPoint(period=period, pass_rate=_rate(total, passed), total_evals=total, passed_evals=passed)


def _compute_trend_direction(trend: list[OkrTrendPoint], threshold: float = 0.05) -> TrendDirection:
    """Determine trend direction from sequential trend points.

    Compares the most recent two non-empty, non-overlapping windows.
    Excludes ``overall`` (which overlaps with all windows). A change of
    less than *threshold* (default 0.05) is considered stable.
    """
    discrete = [p for p in trend if p.total_evals > 0 and p.period != "overall"]
    if len(discrete) >= 2:
        latest = discrete[-1].pass_rate
        previous = discrete[-2].pass_rate
    else:
        points_with_data = [p for p in trend if p.total_evals > 0]
        if len(points_with_data) < 2:
            return "stable"
        latest = points_with_data[-1].pass_rate
        previous = points_with_data[-2].pass_rate
    delta = latest - previous

    if delta <= -threshold:
        return "declining"
    if delta >= threshold:
        return "improving"
    return "stable"


def _days_between(from_date: datetime, target_date_str: str | None) -> int | None:
    """Calculate days between *from_date* and the target date string (ISO 8601)."""
    if target_date_str is None:
        return None
    try:
        target = datetime.strptime(target_date_str, "%Y-%m-%d").replace(tzinfo=UTC)
        delta = target - from_date
        return max(0, delta.days)
    except ValueError:
        _log.warning("Invalid target date for OKR days calculation: %s", target_date_str)
        return None


async def track_okr_progress(
    session: AsyncSession,
    org_id: UUID,
    suite_id: str,
    *,
    target_date: str | None = None,
) -> OkrProgress:
    """Query eval results for a suite and compute OKR progress.

    Queries all eval definitions matching *suite_id*, buckets their results
    into sequential time windows (7d, 14d, 30d, overall), and returns a
    progress snapshot including trend direction and breach status.

    Args:
        session: Async DB session (must have active transaction).
        org_id: Organisation to scope the query.
        suite_id: The eval suite identifier.
        target_date: Optional ISO 8601 date (e.g. ``"2026-09-30"``)
            to compute days-to-target.

    Returns:
        OkrProgress with current score, trend, and breach status.

    Raises:
        ValueError: If no eval definitions exist with the given suite_id.
        SQLAlchemyError: If a database error occurs.

    """
    if not suite_id:
        raise ValueError("suite_id must not be empty")

    as_of = datetime.now(UTC)

    try:
        info_q = text("""
            SELECT
                COUNT(*) AS def_count,
                MAX(pass_threshold) AS pass_threshold
            FROM eval_definitions
            WHERE suite_id = :suite_id AND organisation_id = :org_id
        """)
        info_row = (await session.execute(info_q, {"suite_id": suite_id, "org_id": org_id})).first()
        if info_row is None or info_row.def_count == 0:
            raise ValueError(f"Suite {suite_id!r} not found for organisation {org_id}")

        pass_threshold = info_row.pass_threshold

        window_7 = as_of - timedelta(days=7)
        window_14 = as_of - timedelta(days=14)
        window_30 = as_of - timedelta(days=30)

        trend_q = text("""
            WITH suite_eval_ids AS (
                SELECT id FROM eval_definitions
                WHERE suite_id = :suite_id AND organisation_id = :org_id
                  AND eval_type != 'guardrail'
            )
            SELECT
                SUM(CASE WHEN er.evaluated_at >= :window_7 THEN 1 ELSE 0 END)
                    AS total_7d,
                SUM(CASE WHEN er.evaluated_at >= :window_7 AND er.passed THEN 1 ELSE 0 END)
                    AS passed_7d,
                SUM(
                    CASE WHEN er.evaluated_at >= :window_14 AND er.evaluated_at < :window_7 THEN 1 ELSE 0 END
                ) AS total_14d,
                SUM(
                    CASE
                        WHEN er.evaluated_at >= :window_14 AND er.evaluated_at < :window_7 AND er.passed THEN 1 ELSE 0
                    END
                ) AS passed_14d,
                SUM(CASE WHEN er.evaluated_at >= :window_30 AND er.evaluated_at < :window_14 THEN 1 ELSE 0 END)
                    AS total_30d,
                SUM(
                    CASE
                        WHEN er.evaluated_at >= :window_30 AND er.evaluated_at < :window_14 AND er.passed THEN 1 ELSE 0
                    END
                ) AS passed_30d,
                COUNT(*) AS total_all,
                SUM(CASE WHEN er.passed THEN 1 ELSE 0 END) AS passed_all
            FROM eval_results er
            WHERE er.eval_id IN (SELECT id FROM suite_eval_ids)
              AND er.organisation_id = :org_id
        """)

        trend_params = {
            "suite_id": suite_id,
            "org_id": org_id,
            "window_7": window_7,
            "window_14": window_14,
            "window_30": window_30,
        }
        trend_row = (await session.execute(trend_q, trend_params)).first()
    except TimeoutError:
        _log.exception(
            "OKR progress query timed out for suite %s (org %s)",
            _sanitise_log_value(suite_id),
            _sanitise_log_value(org_id),
        )
        raise
    except SQLAlchemyError:
        _log.exception(
            "OKR progress DB error for suite %s (org %s)",
            _sanitise_log_value(suite_id),
            _sanitise_log_value(org_id),
        )
        raise

    total_7d = trend_row.total_7d or 0 if trend_row else 0
    passed_7d = trend_row.passed_7d or 0 if trend_row else 0
    total_14d = trend_row.total_14d or 0 if trend_row else 0
    passed_14d = trend_row.passed_14d or 0 if trend_row else 0
    total_30d = trend_row.total_30d or 0 if trend_row else 0
    passed_30d = trend_row.passed_30d or 0 if trend_row else 0
    total_all = trend_row.total_all or 0 if trend_row else 0
    passed_all = trend_row.passed_all or 0 if trend_row else 0

    trend = [
        _trend_point("7d", total_7d, passed_7d),
        _trend_point("14d", total_14d, passed_14d),
        _trend_point("30d", total_30d, passed_30d),
        _trend_point("overall", total_all, passed_all),
    ]

    # Use the most recent non-empty discrete window; fall back to overall
    scored = [t for t in trend if t.total_evals > 0 and t.period != "overall"]
    current_score = scored[0].pass_rate if scored else trend[3].pass_rate
    trend_direction = _compute_trend_direction(trend)
    days_to_target = _days_between(as_of, target_date)
    breach = alert_on_breach(pass_threshold, current_score) if pass_threshold is not None else False

    return OkrProgress(
        suite_id=suite_id,
        suite_name=suite_id,
        current_score=current_score,
        pass_threshold=pass_threshold,
        trend=trend,
        trend_direction=trend_direction,
        days_to_target=days_to_target,
        breach=breach,
    )


def alert_on_breach(pass_threshold: float, current_pass_rate: float) -> bool:
    """Check if *current_pass_rate* is below *pass_threshold*.

    Args:
        pass_threshold: Minimum acceptable pass rate (0.0-1.0).
        current_pass_rate: Observed pass rate (0.0-1.0).

    Returns:
        True if the current pass rate is below the threshold.

    """
    return current_pass_rate < pass_threshold
