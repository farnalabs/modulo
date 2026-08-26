"""Weekly quality report for Slack — run volume, eval pass rate, cost summary, week-over-week deltas.

All functions assume an active transaction with RLS org context set by the caller.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.reports.scheduler import _deliver_to_urls
from modulo.db.crud.eval_run import non_guardrail_eval_results_clause
from modulo.db.models.daily_run_count import OrgDailyRunCount
from modulo.db.models.eval_result import EvalResult

_log = logging.getLogger(__name__)

_REPORT_PERIOD_DAYS = 7
_WEEK_OVER_WEEK_THRESHOLD = 5.0


async def generate_quality_report(
    session: AsyncSession,
    org_id: uuid.UUID,
    _config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Generate a weekly quality report for the organisation.

    Queries the last 7 days of run data and computes:
      - Total runs (current week)
      - Average eval pass rate (current week)
      - Total cost (current week)
      - Week-over-week deltas for each metric

    Returns a structured dict with report data.
    """
    try:
        today = datetime.now(UTC).date()
        current_start = today - timedelta(days=_REPORT_PERIOD_DAYS - 1)
        previous_start = today - timedelta(days=2 * _REPORT_PERIOD_DAYS - 1)
        previous_end = today - timedelta(days=_REPORT_PERIOD_DAYS)

        current_weekly = await _query_weekly_agg(session, org_id, current_start, today)
        previous_weekly = await _query_weekly_agg(session, org_id, previous_start, previous_end)

        current_eval = await _query_eval_summary(session, org_id, current_start, today)
        previous_eval = await _query_eval_summary(session, org_id, previous_start, previous_end)

        daily_query = (
            select(
                OrgDailyRunCount.run_date,
                func.sum(OrgDailyRunCount.run_count).label("run_count"),
                func.sum(OrgDailyRunCount.total_spend_usd).label("total_spend"),
            )
            .where(
                OrgDailyRunCount.organisation_id == org_id,
                OrgDailyRunCount.run_date >= current_start,
            )
            .group_by(OrgDailyRunCount.run_date)
            .order_by(OrgDailyRunCount.run_date)
        )
        daily_rows = (await session.execute(daily_query)).all()
        daily_map: dict[date, tuple[int, float]] = {}
        for row in daily_rows:
            daily_map[row.run_date] = (
                int(row.run_count) if row.run_count else 0,
                float(row.total_spend) if row.total_spend else 0.0,
            )

        daily_eval = await _query_daily_eval_rates(session, org_id, current_start, today)
        daily_eval_map = {d: round(passed / total * 100, 1) if total > 0 else None for d, total, passed in daily_eval}

        trend: list[dict[str, Any]] = []
        for i in range(_REPORT_PERIOD_DAYS):
            d = current_start + timedelta(days=i)
            rc, sp = daily_map.get(d, (0, 0.0))
            trend.append(
                {
                    "date": d.isoformat(),
                    "run_count": rc,
                    "eval_pass_rate": daily_eval_map.get(d),
                    "token_spend_usd": sp,
                }
            )

        current_runs = current_weekly["run_count"]
        previous_runs = previous_weekly["run_count"]
        current_cost = current_weekly["total_spend"]
        previous_cost = previous_weekly["total_spend"]

        current_avg_rate = current_eval["pass_rate"]
        previous_avg_rate = previous_eval["pass_rate"]
    except SQLAlchemyError:
        _log.exception("Failed to query report data for org %s", org_id)
        raise
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("Unexpected error generating quality report for org %s", org_id)
        raise

    return {
        "period": {
            "start": current_start.isoformat(),
            "end": today.isoformat(),
            "previous_start": previous_start.isoformat(),
            "previous_end": previous_end.isoformat(),
        },
        "summary": {
            "total_runs": current_runs,
            "avg_eval_pass_rate": current_avg_rate,
            "total_cost_usd": current_cost,
        },
        "week_over_week": {
            "runs_delta_pct": _pct_delta(float(current_runs), float(previous_runs)),
            "eval_pass_rate_delta_pct": (
                _pct_delta(current_avg_rate, previous_avg_rate)
                if current_avg_rate is not None and previous_avg_rate is not None
                else None
            ),
            "cost_delta_pct": _pct_delta(current_cost, previous_cost),
            "previous_week_runs": previous_runs,
            "previous_week_avg_pass_rate": previous_avg_rate,
            "previous_week_cost_usd": previous_cost,
        },
        "trend": trend,
        "eval_breakdown": {
            "current_week": {
                "total_evals": current_eval["total_evals"],
                "passed_evals": current_eval["passed_evals"],
                "pass_rate": current_avg_rate,
            },
            "previous_week": {
                "total_evals": previous_eval["total_evals"],
                "passed_evals": previous_eval["passed_evals"],
                "pass_rate": previous_avg_rate,
            },
        },
    }


async def _query_weekly_agg(
    session: AsyncSession,
    org_id: uuid.UUID,
    start: date,
    end: date,
) -> dict[str, Any]:
    q = select(
        func.sum(OrgDailyRunCount.run_count).label("run_count"),
        func.sum(OrgDailyRunCount.total_spend_usd).label("total_spend"),
    ).where(
        OrgDailyRunCount.organisation_id == org_id,
        OrgDailyRunCount.run_date.between(start, end),
        OrgDailyRunCount.team_id.is_(None),
    )
    result = await session.execute(q)
    row = result.one()
    return {
        "run_count": int(row.run_count) if row.run_count else 0,
        "total_spend": float(row.total_spend) if row.total_spend else 0.0,
    }


def _date_to_dt(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=UTC)


async def _query_eval_summary(
    session: AsyncSession,
    org_id: uuid.UUID,
    start: date,
    end: date,
) -> dict[str, Any]:
    start_dt = _date_to_dt(start)
    end_dt = _date_to_dt(end) + timedelta(days=1)
    q = select(
        func.count().label("total_evals"),
        func.sum(case((EvalResult.passed.is_(True), 1), else_=0)).label("passed_evals"),
    ).where(
        EvalResult.organisation_id == org_id,
        EvalResult.evaluated_at >= start_dt,
        EvalResult.evaluated_at < end_dt,
        non_guardrail_eval_results_clause(),
    )
    result = await session.execute(q)
    row = result.one()
    total = int(row.total_evals)
    passed = int(row.passed_evals) if row.passed_evals else 0
    return {
        "total_evals": total,
        "passed_evals": passed,
        "pass_rate": round(passed / total * 100, 1) if total > 0 else None,
    }


async def _query_daily_eval_rates(
    session: AsyncSession,
    org_id: uuid.UUID,
    start: date,
    end: date,
) -> list[tuple[date, int, int]]:
    start_dt = _date_to_dt(start)
    end_dt = _date_to_dt(end) + timedelta(days=1)
    eval_date = func.date(EvalResult.evaluated_at)
    q = (
        select(
            eval_date.label("eval_date"),
            func.count().label("total"),
            func.sum(case((EvalResult.passed.is_(True), 1), else_=0)).label("passed"),
        )
        .where(
            EvalResult.organisation_id == org_id,
            EvalResult.evaluated_at >= start_dt,
            EvalResult.evaluated_at < end_dt,
            non_guardrail_eval_results_clause(),
        )
        .group_by(eval_date)
        .order_by(eval_date)
    )
    result = await session.execute(q)
    return [(row.eval_date, int(row.total), int(row.passed)) for row in result.all()]


def _fmt_pct(value: float | None) -> str:
    return f"{value}%" if value is not None else "\u2014"


def _pct_delta(current: float, previous: float) -> float | None:
    if previous == 0:
        return None
    return round((current - previous) / previous * 100, 1)


_TREND_UP = "\u2191"
_TREND_DOWN = "\u2193"
_TREND_FLAT = "\u2192"


def _trend_symbol(delta_pct: float | None, *, invert: bool = False) -> str:
    """Return the week-over-week trend arrow for a delta percentage.

    Defaults to "higher is better" semantics: a positive delta renders an up
    arrow, a negative delta a down arrow. When ``invert`` is True the arrow is
    flipped, so a *negative* delta (an improvement for metrics where lower is
    better, e.g. cost) renders an up arrow.
    """
    if delta_pct is None or abs(delta_pct) <= _WEEK_OVER_WEEK_THRESHOLD:
        return _TREND_FLAT
    if invert:
        return _TREND_DOWN if delta_pct > 0 else _TREND_UP
    return _TREND_UP if delta_pct > 0 else _TREND_DOWN


def _format_summary_block(summary: dict[str, Any]) -> dict[str, Any]:
    rate_str = _fmt_pct(summary["avg_eval_pass_rate"])
    return {
        "type": "section",
        "fields": [
            {"type": "mrkdwn", "text": f"*Total Runs*\n{summary['total_runs']}"},
            {"type": "mrkdwn", "text": f"*Avg Eval Pass Rate*\n{rate_str}"},
            {"type": "mrkdwn", "text": f"*Total Cost*\n${summary['total_cost_usd']:.2f}"},
        ],
    }


def _format_trend_section(wow: dict[str, Any], summary: dict[str, Any]) -> dict[str, Any]:
    rate_str = _fmt_pct(summary["avg_eval_pass_rate"])
    prev_rate = wow["previous_week_avg_pass_rate"]
    prev_rate_str = _fmt_pct(prev_rate)

    runs_line = (
        f"{_trend_symbol(wow['runs_delta_pct'])} *Runs*: {summary['total_runs']} "
        f"(prev: {wow['previous_week_runs']}, \u0394 {_fmt_delta(wow['runs_delta_pct'])})"
    )
    eval_line = (
        f"{_trend_symbol(wow['eval_pass_rate_delta_pct'])} *Eval Pass Rate*: "
        f"{rate_str} (prev: {prev_rate_str}, "
        f"\u0394 {_fmt_delta(wow['eval_pass_rate_delta_pct'])})"
    )
    cost_line = (
        f"{_trend_symbol(wow['cost_delta_pct'], invert=True)} *Cost*: "
        f"${summary['total_cost_usd']:.2f} "
        f"(prev: ${wow['previous_week_cost_usd']:.2f}, "
        f"\u0394 {_fmt_delta(wow['cost_delta_pct'])})"
    )
    lines = [runs_line, eval_line, cost_line]
    return {
        "type": "section",
        "text": {"type": "mrkdwn", "text": "*Week-over-Week Deltas*\n" + "\n".join(lines)},
    }


def _fmt_delta(delta_pct: float | None) -> str:
    if delta_pct is None:
        return "N/A"
    return f"{delta_pct:+.1f}%"


def _format_eval_breakdown(eval_bd: dict[str, Any]) -> dict[str, Any]:
    cw = eval_bd["current_week"]
    pw = eval_bd["previous_week"]
    cw_rate = _fmt_pct(cw["pass_rate"])
    pw_rate = _fmt_pct(pw["pass_rate"])
    return {
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": (
                f"*Eval Breakdown*\n"
                f"\u2022 This week: {cw['passed_evals']}/{cw['total_evals']} passed ({cw_rate})\n"
                f"\u2022 Last week: {pw['passed_evals']}/{pw['total_evals']} passed ({pw_rate})"
            ),
        },
    }


def _format_trend_block(trend: list[dict[str, Any]]) -> dict[str, Any]:
    lines = ["*Daily Trend (last 7 days)*"]
    for entry in trend:
        rate_str = _fmt_pct(entry["eval_pass_rate"])
        lines.append(
            f"\u2022 {entry['date']}: {entry['run_count']} runs, {rate_str} pass, ${entry['token_spend_usd']:.2f}"
        )
    return {
        "type": "section",
        "text": {"type": "mrkdwn", "text": "\n".join(lines)},
    }


def format_slack_message(report: dict[str, Any]) -> str:
    """Format a quality report as Slack blocks JSON.

    Returns a JSON string suitable for use as the ``blocks`` field in
    a Slack webhook payload (``{"blocks": <result>}``).
    """
    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "Weekly Quality Report"},
        },
        {
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": f"Period: {report['period']['start']} \u2192 {report['period']['end']}"}
            ],
        },
        {"type": "divider"},
        _format_summary_block(report["summary"]),
        {"type": "divider"},
        _format_trend_section(report["week_over_week"], report["summary"]),
        {"type": "divider"},
        _format_eval_breakdown(report["eval_breakdown"]),
        {"type": "divider"},
        _format_trend_block(report["trend"]),
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": "Generated by Modulo Quality Report"}],
        },
    ]
    return json.dumps(blocks)


async def deliver_quality_report(
    report_data: dict[str, Any] | str,
    recipient_config: dict[str, Any],
) -> list[dict[str, Any]]:
    """Deliver a formatted quality report to Slack webhook URLs.

    Accepts either a raw report dict (from ``generate_quality_report``) or a
    pre-formatted JSON string (when called via the scheduler's formatter
    pipeline). Handles both cases transparently.

    Args:
        report_data: The report dict from ``generate_quality_report`` or a
            pre-formatted Slack blocks JSON string.
        recipient_config: Config dict with ``webhook_urls`` list.

    Returns a list of delivery results with keys: url, status, status_code, error.

    Optional ``recipient_config`` keys:
      - ``signing_secret``: when set, the payload is HMAC-SHA256 signed and the
        signature sent as ``X-Modulo-Signature`` (PRD 8.11 webhook signing).
      - ``timeout``: per-request timeout in seconds (default 30s).

    """
    slack_blocks_str = report_data if isinstance(report_data, str) else format_slack_message(report_data)
    payload = {"blocks": json.loads(slack_blocks_str)}
    return await _deliver_to_urls(
        recipient_config.get("webhook_urls", []),
        payload,
        signing_secret=recipient_config.get("signing_secret"),
        request_timeout=recipient_config.get("timeout"),
    )
