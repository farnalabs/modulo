"""Weekly quality report for Slack — run volume, eval pass rate, cost summary, week-over-week deltas.

All functions assume an active transaction with RLS org context set by the caller.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.models.daily_run_count import OrgDailyRunCount
from modulo.db.models.eval_result import EvalResult

_REPORT_PERIOD_DAYS = 7


async def generate_quality_report(
    session: AsyncSession,
    org_id: uuid.UUID,
) -> dict[str, Any]:
    """Generate a weekly quality report for the organisation.

    Queries the last 7 days of run data and computes:
      - Total runs (current week)
      - Average eval pass rate (current week)
      - Total cost (current week)
      - Week-over-week deltas for each metric

    Returns a structured dict with report data.
    """
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
    daily_eval_map: dict[date, float | None] = {}
    for d, total, passed in daily_eval:
        daily_eval_map[d] = round(passed / total * 100, 1) if total > 0 else None

    trend: list[dict[str, Any]] = []
    for i in range(_REPORT_PERIOD_DAYS):
        d = current_start + timedelta(days=i)
        rc, sp = daily_map.get(d, (0, 0.0))
        trend.append({
            "date": d.isoformat(),
            "run_count": rc,
            "eval_pass_rate": daily_eval_map.get(d),
            "token_spend_usd": sp,
        })

    current_runs = current_weekly["run_count"]
    previous_runs = previous_weekly["run_count"]
    current_cost = current_weekly["total_spend"]
    previous_cost = previous_weekly["total_spend"]

    current_avg_rate = current_eval["pass_rate"]
    previous_avg_rate = previous_eval["pass_rate"]

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
    q = (
        select(
            func.sum(OrgDailyRunCount.run_count).label("run_count"),
            func.sum(OrgDailyRunCount.total_spend_usd).label("total_spend"),
        )
        .where(
            OrgDailyRunCount.organisation_id == org_id,
            OrgDailyRunCount.run_date.between(start, end),
            OrgDailyRunCount.team_id.is_(None),
        )
    )
    result = await session.execute(q)
    row = result.one()
    return {
        "run_count": int(row.run_count) if row.run_count else 0,
        "total_spend": float(row.total_spend) if row.total_spend else 0.0,
    }


async def _query_eval_summary(
    session: AsyncSession,
    org_id: uuid.UUID,
    start: date,
    end: date,
) -> dict[str, Any]:
    q = (
        select(
            func.count().label("total_evals"),
            func.sum(case((EvalResult.passed == True, 1), else_=0)).label("passed_evals"),  # noqa: E712
        )
        .where(
            EvalResult.organisation_id == org_id,
            func.date(EvalResult.evaluated_at).between(start, end),
        )
    )
    result = await session.execute(q)
    row = result.one()
    total = int(row.total_evals)
    passed = int(row.passed_evals)
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
    q = (
        select(
            func.date(EvalResult.evaluated_at).label("eval_date"),
            func.count().label("total"),
            func.sum(case((EvalResult.passed == True, 1), else_=0)).label("passed"),  # noqa: E712
        )
        .where(
            EvalResult.organisation_id == org_id,
            func.date(EvalResult.evaluated_at).between(start, end),
        )
        .group_by(func.date(EvalResult.evaluated_at))
        .order_by(func.date(EvalResult.evaluated_at))
    )
    result = await session.execute(q)
    return [
        (row.eval_date, int(row.total), int(row.passed))
        for row in result.all()
    ]


def _pct_delta(current: float, previous: float) -> float | None:
    if previous == 0:
        return None
    return round((current - previous) / previous * 100, 1)


_TREND_UP = "\u2191"
_TREND_DOWN = "\u2193"
_TREND_FLAT = "\u2192"


def _trend_symbol(delta_pct: float | None, *, invert: bool = False) -> str:
    if delta_pct is None:
        return _TREND_FLAT
    threshold = 5.0
    large_up = delta_pct > threshold
    small_up = 0 < delta_pct <= threshold
    large_down = delta_pct < -threshold
    small_down = -threshold <= delta_pct < 0
    if invert:
        large_up, large_down = large_down, large_up
        small_up, small_down = small_down, small_up
    if large_down:
        return _TREND_DOWN
    if large_up:
        return _TREND_UP
    if small_down:
        return _TREND_DOWN
    if small_up:
        return _TREND_UP
    return _TREND_FLAT


def _format_summary_block(summary: dict[str, Any]) -> dict[str, Any]:
    rate_str = f"{summary['avg_eval_pass_rate']}%" if summary['avg_eval_pass_rate'] is not None else "\u2014"
    return {
        "type": "section",
        "fields": [
            {"type": "mrkdwn", "text": f"*Total Runs*\n{summary['total_runs']}"},
            {"type": "mrkdwn", "text": f"*Avg Eval Pass Rate*\n{rate_str}"},
            {"type": "mrkdwn", "text": f"*Total Cost*\n${summary['total_cost_usd']:.2f}"},
        ],
    }


def _format_trend_section(wow: dict[str, Any], summary: dict[str, Any]) -> dict[str, Any]:
    rate_str = f"{summary['avg_eval_pass_rate']}%" if summary['avg_eval_pass_rate'] is not None else "\u2014"
    prev_rate = wow["previous_week_avg_pass_rate"]
    prev_rate_str = f"{prev_rate}%" if prev_rate is not None else "\u2014"

    runs_line = (
        f"{_trend_symbol(wow['runs_delta_pct'])} *Runs*: {summary['total_runs']} "
        f"(prev: {wow['previous_week_runs']}, \u0394 {_fmt_delta(wow['runs_delta_pct'])})"
    )
    eval_line = (
        f"{_trend_symbol(wow['eval_pass_rate_delta_pct'], invert=True)} *Eval Pass Rate*: "
        f"{rate_str} (prev: {prev_rate_str}, "
        f"\u0394 {_fmt_delta(wow['eval_pass_rate_delta_pct'])})"
    )
    cost_line = (
        f"{_trend_symbol(wow['cost_delta_pct'])} *Cost*: "
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
    cw_rate = f"{cw['pass_rate']}%" if cw['pass_rate'] is not None else "\u2014"
    pw_rate = f"{pw['pass_rate']}%" if pw['pass_rate'] is not None else "\u2014"
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
        rate_str = f"{entry['eval_pass_rate']}%" if entry["eval_pass_rate"] is not None else "\u2014"
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
            "elements": [
                {"type": "mrkdwn", "text": "Generated by Modulo Quality Report"}
            ],
        },
    ]
    return json.dumps(blocks)


async def deliver_quality_report(
    report_data: dict[str, Any],
    recipient_urls: list[str],
) -> list[dict[str, Any]]:
    """Deliver a formatted quality report to Slack webhook URLs.

    Args:
        report_data: The report dict from ``generate_quality_report``.
        recipient_urls: List of Slack webhook URLs to POST to.

    Returns a list of delivery results with keys: url, status, status_code, error.
    """
    slack_blocks_str = format_slack_message(report_data)
    payload = {"blocks": json.loads(slack_blocks_str)}

    results: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=30.0) as client:
        for url in recipient_urls:
            try:
                resp = await client.post(
                    url,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                )
                results.append({
                    "url": url,
                    "status": "delivered" if resp.is_success else "failed",
                    "status_code": resp.status_code,
                    "error": None if resp.is_success else resp.text[:200],
                })
            except httpx.RequestError as exc:
                results.append({
                    "url": url,
                    "status": "failed",
                    "status_code": None,
                    "error": str(exc),
                })
    return results
