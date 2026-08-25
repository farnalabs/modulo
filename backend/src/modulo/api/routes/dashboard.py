"""GET /api/v1/dashboard/summary — org-level dashboard widgets.

Supports an optional ``days`` query param (1..90) that makes the stat
numbers period-scoped: an additive ``period`` block with ``{current, previous,
delta_pct}`` per metric, computed from ``run_daily_facts`` (count/status/tokens/
success/duration), the ``org_daily_run_counts`` ledger (spend) and
``eval_results`` (eval pass rate). When ``days`` is omitted the response is
unchanged (all-time scalars) — non-breaking for existing consumers.
"""

import json
import logging
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import status as http_status  # nosemgrep: loopvar-shadows-import
from redis.asyncio import Redis
from sqlalchemy import Date, case, cast, func, select
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.constants import MSG_FEATURE_NOT_AVAILABLE
from modulo.api.db_error_handling import handle_db_errors
from modulo.api.dependencies import get_db_session, require_permission
from modulo.auth.jwt import TenantPrincipal
from modulo.connectors._safe_int import safe_int as _safe_int
from modulo.core.analytics import compute_delta
from modulo.core.remy.config_service import RemyConfigService
from modulo.db.crud.eval_run import non_guardrail_eval_results_clause
from modulo.db.models.daily_run_count import OrgDailyRunCount
from modulo.db.models.eval_result import EvalResult
from modulo.db.models.feedback_record import FeedbackRecord
from modulo.db.models.hitl_claim import HitlClaim
from modulo.db.models.model_backend import ModelBackend
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.run import Run
from modulo.db.models.run_daily_facts import RunDailyFact
from modulo.db.models.team import Team
from modulo.db.rls import set_rls_org, set_rls_user_context
from modulo.settings import get_settings

_MSG_DATABASE_TEMPORARILY_UNAVAILABLE = "The database is temporarily unavailable."
_CODE_DASHBOARD_DAILY_RUN_COUNTS = "dashboard.daily_run_counts"


_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/dashboard", tags=["dashboard"])


def _safe_float(value: object, default: float = 0.0) -> float:
    """Convert *value* to float, returning *default* for None, NaN, or conversion error."""
    if not isinstance(value, (int, float, str, bytes, bytearray, Decimal)):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


_TRACKED_STATUSES = ("running", "awaiting_human", "failed", "idle")

# Statuses that hold a slot without actively executing — surfaced as ``idle``.
# ``waiting_for_lock`` was excised in migration 0074/0075 (rows backfilled to
# ``pending``), so it must not appear here.
_IDLE_STATUSES = ("pending", "claimed")

# Allowed rolling-window sizes for the period-scoped summary (FAR-92) — any
# 1..90 (matching /trends ge=1, le=90); enforced by the Query params and the
# handler guard below.
_DASHBOARD_CACHE_TTL = 60  # seconds — dashboard summary cached via Redis


def _dashboard_cache_key(org_id: str, days: int | None) -> str:
    """Cache key — the no-days key keeps the legacy flag-off path unchanged."""
    if days is None:
        return f"dashboard:summary:{org_id}"
    return f"dashboard:summary:{org_id}:{days}"


async def _get_cached_dashboard(org_id: str, days: int | None = None) -> dict[str, Any] | None:
    """Read cached dashboard summary from Redis."""
    settings = get_settings()
    redis: Redis | None = None
    try:
        redis = Redis.from_url(
            settings.redis_url, decode_responses=True, socket_connect_timeout=2.0, socket_timeout=2.0
        )
        key = _dashboard_cache_key(org_id, days)
        cached = await redis.get(key)
        if cached:
            cached_data: dict[str, Any] = json.loads(cached)
            return cached_data
    except Exception:
        _log.warning("dashboard.cache_read_failed", exc_info=True)
    finally:
        if redis is not None:
            await redis.aclose()
    return None


async def _set_cached_dashboard(org_id: str, data: dict[str, Any], days: int | None = None) -> None:
    """Write dashboard summary to Redis cache."""
    settings = get_settings()
    redis: Redis | None = None
    try:
        redis = Redis.from_url(
            settings.redis_url, decode_responses=True, socket_connect_timeout=2.0, socket_timeout=2.0
        )
        key = _dashboard_cache_key(org_id, days)
        await redis.setex(key, _DASHBOARD_CACHE_TTL, json.dumps(data, default=str))
    except Exception:
        _log.warning("dashboard.cache_write_failed", exc_info=True)
    finally:
        if redis is not None:
            await redis.aclose()


def _period_metric(current: float | None, previous: float | None) -> dict[str, Any]:
    """``{current, previous, delta_pct}`` for one period-scoped metric.

    ``delta_pct`` is null when the previous window is zero/absent (no baseline),
    when the current window has no data, or when both are zero — see
    ``compute_delta``.
    """
    if current is None:
        # No current-window data → no baseline comparison (delta undefined).
        return {"current": None, "previous": previous, "delta_pct": None}
    return {
        "current": current,
        "previous": previous,
        "delta_pct": compute_delta(previous, current),
    }


async def _facts_window(
    session: AsyncSession,
    org_id: uuid.UUID,
    start_date: date,
    end_date: date,
) -> dict[str, Any]:
    """Aggregate ``run_daily_facts`` for one window (count/tokens/duration/success).

    Window bounds are ``run_date`` day boundaries (``[start_date, end_date)``)
    — the same day-level bucket key the analytics foundation and the spend
    ledger use. Filtering on ``created_at`` (the write/source-run instant)
    would mis-scope backfilled rows, whose ``created_at`` is recent but whose
    ``run_date`` falls in the target window.

    Isolation invariant: the explicit ``organisation_id = :org`` predicate is
    the control (modulo_app is BYPASSRLS; RLS is defense-in-depth only) — same
    invariant the analytics foundation uses.
    """
    row = (
        await session.execute(
            select(
                func.count().label("total"),
                func.count(func.distinct(RunDailyFact.pipeline_id)).label("active_pipelines"),
                func.sum(RunDailyFact.total_tokens).label("tokens"),
                func.avg(RunDailyFact.duration_ms).label("avg_duration_ms"),
                func.count().filter(RunDailyFact.status == "complete").label("complete"),
            ).where(
                RunDailyFact.organisation_id == org_id,
                RunDailyFact.run_date >= start_date,
                RunDailyFact.run_date < end_date,
            )
        )
    ).one()
    total = _safe_int(row.total)
    complete = _safe_int(row.complete)
    return {
        "total_runs": total,
        "active_pipelines": _safe_int(row.active_pipelines),
        "tokens": _safe_int(row.tokens),
        "avg_duration_ms": round(float(row.avg_duration_ms), 1) if row.avg_duration_ms is not None else None,
        "success_rate": round(complete / total * 100, 1) if total > 0 else None,
    }


async def _facts_status_counts(
    session: AsyncSession,
    org_id: uuid.UUID,
    start_date: date,
    end_date: date,
) -> dict[str, int]:
    """Facts count grouped by status for one ``run_date`` window."""
    rows = (
        await session.execute(
            select(RunDailyFact.status, func.count().label("cnt"))
            .where(
                RunDailyFact.organisation_id == org_id,
                RunDailyFact.run_date >= start_date,
                RunDailyFact.run_date < end_date,
            )
            .group_by(RunDailyFact.status)
        )
    ).all()
    return {row.status: _safe_int(row.cnt) for row in rows}


async def _ledger_spend_window(
    session: AsyncSession,
    org_id: uuid.UUID,
    start_date: date,
    end_date: date,
) -> float:
    """Org-level ledger spend (``team_id IS NULL`` row) summed over one window."""
    total = (
        await session.execute(
            select(func.sum(OrgDailyRunCount.total_spend_usd)).where(
                OrgDailyRunCount.organisation_id == org_id,
                OrgDailyRunCount.team_id.is_(None),
                OrgDailyRunCount.run_date >= start_date,
                OrgDailyRunCount.run_date < end_date,
            )
        )
    ).scalar_one()
    return float(total) if total is not None else 0.0


async def _eval_rate_window(
    session: AsyncSession,
    org_id: uuid.UUID,
    window_start: datetime,
    window_end: datetime,
) -> float | None:
    """Eval pass rate (percent) over one window — the series /trends uses."""
    row = (
        await session.execute(
            select(
                func.count().label("total"),
                func.sum(case((EvalResult.passed.is_(True), 1), else_=0)).label("passed"),
            ).where(
                EvalResult.organisation_id == org_id,
                EvalResult.evaluated_at >= window_start,
                EvalResult.evaluated_at < window_end,
                non_guardrail_eval_results_clause(),
            )
        )
    ).one()
    total = int(row.total) if row.total is not None else 0
    passed = int(row.passed) if row.passed is not None else 0
    return round(passed / total * 100, 1) if total > 0 else None


async def _compute_period_metrics(
    session: AsyncSession,
    org_id: uuid.UUID,
    days: int,
) -> dict[str, Any]:
    """Period-scoped metrics with same-source/same-window value AND arrow.

    The current window is the trailing ``days`` day-bucket period ending today
    (exclusive): ``[today - days, today)``; the previous window is the
    immediately-preceding equal-length period ``[today - 2*days, today - days)``.
    Facts and status counts filter on ``run_date`` (the day-level bucket key),
    matching the spend ledger; eval pass rate keeps a ``now``-based rolling
    window on ``evaluated_at`` (a timestamp, not a day bucket). Every metric
    compares the same source over the same window so the value and its trend
    arrow are always consistent.
    """
    today = datetime.now(UTC).date()
    current_start = today - timedelta(days=days)
    current_end = today
    prev_start = today - timedelta(days=2 * days)
    prev_end = today - timedelta(days=days)

    current_facts = await _facts_window(session, org_id, current_start, current_end)
    previous_facts = await _facts_window(session, org_id, prev_start, prev_end)

    current_status = await _facts_status_counts(session, org_id, current_start, current_end)
    previous_status = await _facts_status_counts(session, org_id, prev_start, prev_end)

    current_spend = await _ledger_spend_window(session, org_id, current_start, current_end)
    previous_spend = await _ledger_spend_window(session, org_id, prev_start, prev_end)

    now = datetime.now(UTC)
    current_eval = await _eval_rate_window(session, org_id, now - timedelta(days=days), now)
    previous_eval = await _eval_rate_window(session, org_id, now - timedelta(days=2 * days), now - timedelta(days=days))

    status_metrics = {
        run_status: _period_metric(current_status.get(run_status, 0), previous_status.get(run_status, 0))
        for run_status in _TRACKED_STATUSES
    }

    return {
        "days": days,
        "metrics": {
            "total_runs": _period_metric(current_facts["total_runs"], previous_facts["total_runs"]),
            "active_pipelines": _period_metric(current_facts["active_pipelines"], previous_facts["active_pipelines"]),
            "run_counts_by_status": status_metrics,
            "tokens": _period_metric(current_facts["tokens"], previous_facts["tokens"]),
            "success_rate": _period_metric(current_facts["success_rate"], previous_facts["success_rate"]),
            "avg_duration_ms": _period_metric(current_facts["avg_duration_ms"], previous_facts["avg_duration_ms"]),
            "eval_pass_rate": _period_metric(current_eval, previous_eval),
            "spend": _period_metric(current_spend, previous_spend),
        },
    }


async def _count_active_pipelines(session: AsyncSession, org_id: uuid.UUID) -> int:
    count_query = (
        select(func.count())
        .select_from(Pipeline)
        .where(
            Pipeline.organisation_id == org_id,
            Pipeline.archived_at.is_(None),
            Pipeline.deleted_at.is_(None),
        )
    )
    return (await session.execute(count_query)).scalar_one() or 0


async def _load_status_counts(session: AsyncSession, org_id: uuid.UUID) -> dict[str, int]:
    status_count_query = (
        select(
            Run.status,
            func.count().label("cnt"),
        )
        .select_from(Run)
        .join(Pipeline, Run.pipeline_id == Pipeline.id)
        .where(
            Run.organisation_id == org_id,
            Pipeline.deleted_at.is_(None),
        )
        .group_by(Run.status)
    )
    status_count_rows = (await session.execute(status_count_query)).all()
    status_counts = {row.status: _safe_int(row.cnt) for row in status_count_rows}
    for tracked_status in _TRACKED_STATUSES:
        status_counts.setdefault(tracked_status, 0)
    idle_count = sum(status_counts.get(s, 0) for s in _IDLE_STATUSES)
    status_counts["idle"] = idle_count
    return status_counts


async def _load_teams(session: AsyncSession, org_id: uuid.UUID) -> list[Team]:
    teams_result = await session.execute(
        select(Team).where(Team.organisation_id == org_id, Team.deleted_at.is_(None)).order_by(Team.name)
    )
    return list(teams_result.scalars().all())


async def _load_team_metrics(
    session: AsyncSession,
    org_id: uuid.UUID,
    teams: list[Team],
) -> list[dict[str, Any]]:
    """Build per-team run count / active-pipeline metrics for the summary."""
    team_run_query = (
        select(
            Run.owner_team_id,
            Run.status,
            func.count().label("cnt"),
        )
        .select_from(Run)
        .join(Pipeline, Run.pipeline_id == Pipeline.id)
        .where(
            Run.organisation_id == org_id,
            Run.owner_team_id.is_not(None),
            Pipeline.deleted_at.is_(None),
        )
        .group_by(Run.owner_team_id, Run.status)
    )
    team_run_rows = (await session.execute(team_run_query)).all()

    team_pipeline_query = (
        select(
            Run.owner_team_id,
            func.count(func.distinct(Run.pipeline_id)).label("pipeline_cnt"),
        )
        .select_from(Run)
        .join(Pipeline, Run.pipeline_id == Pipeline.id)
        .where(
            Run.organisation_id == org_id,
            Run.owner_team_id.is_not(None),
            Pipeline.deleted_at.is_(None),
        )
        .group_by(Run.owner_team_id)
    )
    team_pipeline_rows = (await session.execute(team_pipeline_query)).all()

    team_run_data: dict[str, dict[str, int]] = {}
    for tr_row in team_run_rows:
        tid = str(tr_row.owner_team_id)
        team_run_data.setdefault(tid, {})[tr_row.status] = _safe_int(tr_row.cnt)

    team_pipeline_data = {str(tp_row.owner_team_id): int(tp_row.pipeline_cnt) for tp_row in team_pipeline_rows}

    team_metrics: list[dict[str, Any]] = []
    for team in teams:
        tid = str(team.id)
        run_data = team_run_data.get(tid, {})
        team_total = sum(run_data.get(s, 0) for s in _TRACKED_STATUSES)
        team_statuses = {s: run_data.get(s, 0) for s in _TRACKED_STATUSES}
        team_statuses["idle"] = sum(run_data.get(s, 0) for s in _IDLE_STATUSES)

        team_metrics.append(
            {
                "id": tid,
                "name": team.name,
                "total_runs": team_total,
                "active_pipelines": team_pipeline_data.get(tid, 0),
                "run_counts_by_status": team_statuses,
            }
        )
    return team_metrics


async def _load_eval_stats(
    session: AsyncSession,
    org_id: uuid.UUID,
) -> tuple[dict[str, Any] | None, dict[str, dict[str, Any]]]:
    """Load eval pass-rate aggregates (overall, per pipeline, per team+pipeline).

    Returns ``(eval_pass_rate, per_team_eval)`` where ``per_team_eval`` holds the
    aggregated eval stats keyed by team id (used to enrich team metrics).
    """
    eval_totals_query = (
        select(
            func.count().label("total"),
            func.sum(case((EvalResult.passed.is_(True), 1), else_=0)).label("passed"),
        )
        .select_from(EvalResult)
        .where(
            EvalResult.organisation_id == org_id,
            non_guardrail_eval_results_clause(),
        )
    )
    eval_totals_row = (await session.execute(eval_totals_query)).one()
    eval_total = int(eval_totals_row.total) if eval_totals_row.total is not None else 0
    eval_passed = int(eval_totals_row.passed) if eval_totals_row.passed is not None else 0

    # Superset query: per-team-pipeline eval breakdown; derive per-team and per-pipeline client-side
    per_team_pipeline_query = (
        select(
            Run.owner_team_id,
            Run.pipeline_id,
            func.count().label("total"),
            func.sum(case((EvalResult.passed.is_(True), 1), else_=0)).label("passed"),
        )
        .select_from(EvalResult)
        .join(Run, EvalResult.run_id == Run.id)
        .where(
            EvalResult.organisation_id == org_id,
            Run.owner_team_id.is_not(None),
            non_guardrail_eval_results_clause(),
        )
        .group_by(Run.owner_team_id, Run.pipeline_id)
    )
    per_team_pipeline_rows = (await session.execute(per_team_pipeline_query)).all()
    per_team_pipeline: dict[str, dict[str, dict[str, Any]]] = {}
    per_team_eval: dict[str, dict[str, Any]] = {}
    per_pipeline: dict[str, dict[str, Any]] = {}
    for row in per_team_pipeline_rows:
        team_id = str(row.owner_team_id)
        pipeline_id = str(row.pipeline_id)
        total = int(row.total)
        passed = int(row.passed)
        pr = round(passed / total * 100, 1) if total > 0 else 0.0
        per_team_pipeline.setdefault(team_id, {})[pipeline_id] = {
            "total_evals": total,
            "passed_evals": passed,
            "pass_rate": pr,
        }
        # Derive per-team aggregates
        team_entry = per_team_eval.setdefault(team_id, {"total_evals": 0, "passed_evals": 0, "pass_rate": 0.0})  # nosec B105 — numeric zero default, not a password
        team_entry["total_evals"] += total
        team_entry["passed_evals"] += passed
        team_entry["pass_rate"] = (
            round(team_entry["passed_evals"] / team_entry["total_evals"] * 100, 1)
            if team_entry["total_evals"] > 0
            else 0.0
        )
        # Derive per-pipeline aggregates
        pipe_entry = per_pipeline.setdefault(
            pipeline_id,
            {"total_evals": 0, "passed_evals": 0, "pass_rate": 0.0},  # nosec B105 — numeric zero default, not a password
        )
        pipe_entry["total_evals"] += total
        pipe_entry["passed_evals"] += passed
        pipe_entry["pass_rate"] = (
            round(pipe_entry["passed_evals"] / pipe_entry["total_evals"] * 100, 1)
            if pipe_entry["total_evals"] > 0
            else 0.0
        )

    eval_pass_rate: dict[str, Any] | None = None
    if eval_total > 0:
        eval_pass_rate = {
            "overall_pass_rate": round(eval_passed / eval_total * 100, 1),
            "total_evals": eval_total,
            "passed_evals": eval_passed,
            "per_pipeline": per_pipeline,
            "per_team_pipeline": per_team_pipeline,
        }
    return eval_pass_rate, per_team_eval


def _attach_team_eval_rates(team_metrics: list[dict[str, Any]], per_team_eval: dict[str, dict[str, Any]]) -> None:
    """Attach each team's aggregated eval pass-rate data to its metrics entry."""
    for team_entry in team_metrics:
        if team_eval_data := per_team_eval.get(team_entry["id"]):
            team_entry["eval_pass_rate"] = team_eval_data


async def _load_daily_trend(session: AsyncSession, org_id: uuid.UUID) -> list[dict[str, Any]]:
    """Load the last 7 days of run-count / eval-pass / spend figures."""
    today = datetime.now(UTC).date()
    seven_days_ago = today - timedelta(days=6)

    daily_query = (
        select(
            OrgDailyRunCount.run_date,
            func.sum(OrgDailyRunCount.run_count).label("run_count"),
            func.sum(OrgDailyRunCount.total_spend_usd).label("total_spend"),
        )
        .where(
            OrgDailyRunCount.organisation_id == org_id,
            OrgDailyRunCount.run_date >= seven_days_ago,
        )
        .group_by(OrgDailyRunCount.run_date)
        .order_by(OrgDailyRunCount.run_date)
    )
    daily_rows = (await session.execute(daily_query)).all()
    daily_map: dict[date, tuple[int, float]] = {}
    for dr_row in daily_rows:
        daily_map[dr_row.run_date] = (
            int(dr_row.run_count) if dr_row.run_count else 0,
            float(dr_row.total_spend) if dr_row.total_spend else 0.0,
        )

    daily_eval_query = (
        select(
            cast(EvalResult.evaluated_at, Date).label("eval_date"),
            func.count().label("total"),
            func.sum(case((EvalResult.passed.is_(True), 1), else_=0)).label("passed"),
        )
        .where(
            EvalResult.organisation_id == org_id,
            EvalResult.evaluated_at >= seven_days_ago,
            non_guardrail_eval_results_clause(),
        )
        .group_by(cast(EvalResult.evaluated_at, Date))
        .order_by(cast(EvalResult.evaluated_at, Date))
    )
    daily_eval_rows = (await session.execute(daily_eval_query)).all()
    daily_eval_map: dict[date, float | None] = {}
    for de_row in daily_eval_rows:
        total = int(de_row.total)
        passed = int(de_row.passed)
        daily_eval_map[de_row.eval_date] = round(passed / total * 100, 1) if total > 0 else None

    trend: list[dict[str, Any]] = []
    for i in range(7):
        d = seven_days_ago + timedelta(days=i)
        rc, sp = daily_map.get(d, (0, 0.0))
        trend.append(
            {
                "date": d.isoformat(),
                "run_count": rc,
                "eval_pass_rate": daily_eval_map.get(d),
                "token_spend_usd": sp,
            }
        )
    return trend


async def _load_recent_runs(session: AsyncSession, org_id: uuid.UUID) -> list[dict[str, Any]]:
    recent_runs_query = (
        select(
            Run.id,
            Run.run_number,
            Pipeline.name.label("pipeline_name"),
            Run.status,
            Run.created_at,
            Run.trigger_type,
        )
        .join(Pipeline, Run.pipeline_id == Pipeline.id)
        .where(
            Run.organisation_id == org_id,
            Pipeline.deleted_at.is_(None),
        )
        .order_by(Run.created_at.desc())
        .limit(10)
    )
    recent_runs_rows = (await session.execute(recent_runs_query)).all()
    return [
        {
            "id": str(row.id),
            "run_number": row.run_number,
            "pipeline_name": row.pipeline_name,
            "status": row.status,
            "created_at": row.created_at.isoformat(),
            "trigger_type": row.trigger_type,
        }
        for row in recent_runs_rows
    ]


async def _load_config_warnings(session: AsyncSession, org_id: uuid.UUID) -> list[dict[str, Any]]:
    """Collect non-blocking configuration warnings shown on the dashboard."""
    config_warnings: list[dict[str, Any]] = []

    try:
        mb_count_result = await session.execute(
            select(func.count()).select_from(ModelBackend).where(ModelBackend.organisation_id == org_id)
        )
        mb_count = int(mb_count_result.scalar_one())
    except Exception:
        _log.exception("dashboard.dashboard_summary.model_backend_count")
        mb_count = 0

    if mb_count == 0:
        config_warnings.append(
            {
                "type": "no_model_backends",
                "severity": "high",
                "message": ("No AI providers configured. Add a model backend with API credentials to run pipelines."),
                "action_label": "Configure provider",
                "action_url": "/admin/model-backends",
            }
        )
        return config_warnings

    try:
        remy_config = await RemyConfigService(session).get_config(org_id)
        default_provider = remy_config.default_provider
        default_provider_result = await session.execute(
            select(func.count())
            .select_from(ModelBackend)
            .where(
                ModelBackend.organisation_id == org_id,
                ModelBackend.provider == default_provider,
                ModelBackend.credentials_ciphertext != b"",
            )
        )
        default_provider_count = int(default_provider_result.scalar_one())
        if default_provider_count == 0 and mb_count > 1:
            config_warnings.append(
                {
                    "type": "remy_provider_not_configured",
                    "severity": "low",
                    "message": (
                        f"Remy is configured to use {default_provider} but no API key is set "
                        "for that provider. Remy will auto-detect the first configured "
                        "provider. Change the default in Remy Config."
                    ),
                    "action_label": f"Configure {default_provider}",
                    "action_url": "/admin/model-backends",
                }
            )
    except Exception:
        _log.warning("dashboard.config_warnings.remy_failed", exc_info=True)
    return config_warnings


@router.get("/summary")
@handle_db_errors("dashboard.dashboard_summary")
async def dashboard_summary(
    days: int | None = Query(None, ge=1, le=90),
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("dashboard.summary"),
) -> dict[str, Any]:
    """Org-level dashboard summary with counts, team breakdown, eval pass rate, and 7-day trend."""
    try:
        if days is not None and not (1 <= days <= 90):
            raise HTTPException(
                status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="days must be between 1 and 90.",
            )

        org_id_str = str(principal.organisation_id)

        cached = await _get_cached_dashboard(org_id_str, days)
        if cached is not None:
            return cached

        period_metrics: dict[str, Any] | None = None
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)

            org_id = principal.organisation_id

            if days is not None:
                period_metrics = await _compute_period_metrics(session, org_id, days)

            # --- Queries that can all run independently (no dependencies between them) ---
            active_pipelines = await _count_active_pipelines(session, org_id)
            status_counts = await _load_status_counts(session, org_id)
            teams = await _load_teams(session, org_id)
            team_metrics = await _load_team_metrics(session, org_id, teams)
            eval_pass_rate, per_team_eval = await _load_eval_stats(session, org_id)
            _attach_team_eval_rates(team_metrics, per_team_eval)
            trend = await _load_daily_trend(session, org_id)
            recent_runs = await _load_recent_runs(session, org_id)
            config_warnings = await _load_config_warnings(session, org_id)

        total_runs = sum(v for k, v in status_counts.items() if k not in _IDLE_STATUSES)
        result: dict[str, Any] = {
            "total_runs": total_runs,
            "active_pipelines": active_pipelines,
            "run_counts_by_status": status_counts,
            "teams": team_metrics,
            "eval_pass_rate": eval_pass_rate,
            "trend": trend,
            "recent_runs": recent_runs,
            "config_warnings": config_warnings,
        }
        if period_metrics is not None:
            result["period"] = period_metrics

        await _set_cached_dashboard(org_id_str, result, days)
        return result
    except ProgrammingError as exc:
        _log.exception("dashboard.dashboard_summary")
        raise HTTPException(
            status_code=http_status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from exc
    except SQLAlchemyError as exc:
        _log.exception("dashboard.dashboard_summary")
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_TEMPORARILY_UNAVAILABLE,
        ) from exc
    except HTTPException:
        # Preserve the intended status for the days validation (422) instead of
        # collapsing it into a 500 via the generic handler below.
        raise
    except Exception as exc:
        _log.exception("dashboard.summary_failed")
        raise HTTPException(
            status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while loading the dashboard.",
        ) from exc


async def _load_trend_eval_rates(session: AsyncSession, org_id: uuid.UUID, start_date: date) -> list[dict[str, Any]]:
    eval_query = (
        select(
            cast(EvalResult.evaluated_at, Date).label("eval_date"),
            func.count().label("total"),
            func.sum(case((EvalResult.passed.is_(True), 1), else_=0)).label("passed"),
        )
        .where(
            EvalResult.organisation_id == org_id,
            EvalResult.evaluated_at >= start_date,
            non_guardrail_eval_results_clause(),
        )
        .group_by(cast(EvalResult.evaluated_at, Date))
        .order_by(cast(EvalResult.evaluated_at, Date))
    )
    eval_result = await session.execute(eval_query)
    eval_rates: list[dict[str, Any]] = []
    for row in eval_result.all():
        total = int(row.total)
        passed = int(row.passed)
        eval_rates.append(
            {
                "date": str(row.eval_date),
                "total_evals": total,
                "passed_evals": passed,
                "pass_rate": round(passed / total * 100, 1) if total > 0 else None,
            }
        )
    return eval_rates


async def _load_trend_run_and_spend(
    session: AsyncSession, org_id: uuid.UUID, start_date: date
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    daily_query = (
        select(
            OrgDailyRunCount.run_date,
            func.sum(OrgDailyRunCount.run_count).label("run_count"),
            func.sum(OrgDailyRunCount.total_spend_usd).label("total_spend"),
        )
        .where(
            OrgDailyRunCount.organisation_id == org_id,
            OrgDailyRunCount.run_date >= start_date,
        )
        .group_by(OrgDailyRunCount.run_date)
        .order_by(OrgDailyRunCount.run_date)
    )
    daily_result = await session.execute(daily_query)
    all_rows = daily_result.all()
    run_counts = [{"date": str(row.run_date), "run_count": int(row.run_count)} for row in all_rows]
    token_spend = [
        {"date": str(row.run_date), "total_spend_usd": float(row.total_spend) if row.total_spend else 0.0}
        for row in all_rows
    ]
    return run_counts, token_spend


async def _load_hitl_series(
    session: AsyncSession,
    org_id: uuid.UUID,
    start_date: date,
    days: int,
    eval_rates: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Build the HITL volume / rejection-rate / correlation series for the trend window (§8.20)."""
    hitl_decision_query = (
        select(
            cast(HitlClaim.decision_at, Date).label("decision_date"),
            func.count().label("total_decisions"),
            func.sum(case((HitlClaim.decision == "approved", 1), else_=0)).label("approved_count"),
            func.sum(case((HitlClaim.decision == "rejected", 1), else_=0)).label("rejected_count"),
            func.avg(func.extract("epoch", HitlClaim.decision_at - HitlClaim.created_at) * 1000).label(
                "avg_time_to_approve_ms"
            ),
        )
        .where(
            HitlClaim.organisation_id == org_id,
            HitlClaim.decision.is_not(None),
            HitlClaim.decision_at.is_not(None),
            HitlClaim.created_at >= start_date,
        )
        .group_by(cast(HitlClaim.decision_at, Date))
        .order_by(cast(HitlClaim.decision_at, Date))
    )
    hitl_rows = (await session.execute(hitl_decision_query)).all()

    hitl_by_date: dict[str, dict[str, Any]] = {}
    for row in hitl_rows:
        d = str(row.decision_date)
        total = int(row.total_decisions)
        approved = int(row.approved_count)
        rejected = int(row.rejected_count)
        hitl_by_date[d] = {
            "total_decisions": total,
            "approved_count": approved,
            "rejected_count": rejected,
            "rejection_rate": round(rejected / total * 100, 1) if total > 0 else 0.0,
            "avg_time_to_approve_ms": (
                round(float(row.avg_time_to_approve_ms), 1) if row.avg_time_to_approve_ms else None
            ),
        }

    hitl_volume: list[dict[str, Any]] = []
    for i in range(min(days, 90)):
        d = (start_date + timedelta(days=i)).isoformat()
        entry = hitl_by_date.get(
            d,
            {
                "total_decisions": 0,
                "approved_count": 0,
                "rejected_count": 0,
                "rejection_rate": 0.0,
                "avg_time_to_approve_ms": None,
            },
        )
        entry["date"] = d
        hitl_volume.append(entry)

    # Rejection-rate trend (rolling 3-day average for smoothing)
    raw_rates = [h["rejection_rate"] for h in hitl_volume]
    rejection_trend: list[dict[str, Any]] = []
    for i, h in enumerate(hitl_volume):
        window = raw_rates[max(0, i - 2) : i + 1]
        smoothed = round(sum(window) / len(window), 1) if window else 0.0
        rejection_trend.append(
            {
                "date": h["date"],
                "rolling_rejection_rate": smoothed,
                "raw_rejection_rate": h["rejection_rate"],
            }
        )

    # Correlation: eval pass rate vs rejection rate per day
    eval_rate_map: dict[str, float | None] = {r["date"]: r.get("pass_rate") for r in eval_rates}
    correlation = [
        {
            "date": h["date"],
            "rejection_rate": h["rejection_rate"],
            "eval_pass_rate": eval_rate_map.get(h["date"]),
        }
        for h in hitl_volume
    ]
    return hitl_volume, rejection_trend, correlation


async def _load_feedback_volume(
    session: AsyncSession, org_id: uuid.UUID, start_date: date, days: int
) -> list[dict[str, Any]]:
    feedback_volume_query = (
        select(
            cast(FeedbackRecord.created_at, Date).label("feedback_date"),
            func.count().label("feedback_count"),
            func.sum(case((FeedbackRecord.feedback_status == "resolved", 1), else_=0)).label("resolved_count"),
            func.sum(case((FeedbackRecord.feedback_status == "correcting", 1), else_=0)).label("correcting_count"),
        )
        .where(
            FeedbackRecord.organisation_id == org_id,
            FeedbackRecord.created_at >= start_date,
        )
        .group_by(cast(FeedbackRecord.created_at, Date))
        .order_by(cast(FeedbackRecord.created_at, Date))
    )
    feedback_rows = (await session.execute(feedback_volume_query)).all()

    feedback_by_date: dict[str, dict[str, Any]] = {}
    for row in feedback_rows:
        feedback_by_date[str(row.feedback_date)] = {
            "feedback_count": int(row.feedback_count),
            "resolved_count": int(row.resolved_count),
            "correcting_count": int(row.correcting_count),
        }

    feedback_volume: list[dict[str, Any]] = []
    for i in range(min(days, 90)):
        d = (start_date + timedelta(days=i)).isoformat()
        entry = feedback_by_date.get(
            d,
            {
                "feedback_count": 0,
                "resolved_count": 0,
                "correcting_count": 0,
            },
        )
        entry["date"] = d
        feedback_volume.append(entry)
    return feedback_volume


@router.get("/trends")
@handle_db_errors("dashboard.dashboard_trends")
async def dashboard_trends(
    days: int = Query(7, ge=1, le=90),
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("dashboard.trends"),
) -> dict[str, Any]:
    """Trend data over the specified number of days — run counts, eval pass rate, token spend."""
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)

            org_id = principal.organisation_id
            today = datetime.now(UTC).date()
            start_date = today - timedelta(days=days - 1)

            eval_rates = await _load_trend_eval_rates(session, org_id, start_date)
            run_counts, token_spend = await _load_trend_run_and_spend(session, org_id, start_date)
            hitl_volume, rejection_trend, correlation = await _load_hitl_series(
                session, org_id, start_date, days, eval_rates
            )
            feedback_volume = await _load_feedback_volume(session, org_id, start_date, days)

        return {
            "days": days,
            "run_counts": run_counts,
            "eval_pass_rates": eval_rates,
            "token_spend": token_spend,
            "hitl_volume": hitl_volume,
            "rejection_trend": rejection_trend,
            "correlation": correlation,
            "feedback_volume": feedback_volume,
        }
    except ProgrammingError as exc:
        _log.exception("dashboard.dashboard_trends")
        raise HTTPException(
            status_code=http_status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from exc
    except SQLAlchemyError as exc:
        _log.exception("dashboard.dashboard_trends")
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_TEMPORARILY_UNAVAILABLE,
        ) from exc
    except Exception as exc:
        _log.exception("dashboard.trends_failed")
        raise HTTPException(
            status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while loading trends.",
        ) from exc


@router.get("/daily-run-counts")
@handle_db_errors(_CODE_DASHBOARD_DAILY_RUN_COUNTS)
async def daily_run_counts(
    days: int = Query(30, ge=1, le=365),
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_DASHBOARD_DAILY_RUN_COUNTS),
) -> dict[str, Any]:
    """Return daily run counts for the last N days, grouped by status."""
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)

            cutoff = datetime.now(UTC) - timedelta(days=days)

            result = await session.execute(
                select(
                    cast(Run.created_at, Date).label("day"),
                    Run.status,
                    func.count().label("cnt"),
                )
                .where(
                    Run.organisation_id == principal.organisation_id,
                    Run.created_at >= cutoff,
                )
                .group_by(cast(Run.created_at, Date), Run.status)
                .order_by(cast(Run.created_at, Date))
            )

        daily: dict[str, dict[str, int]] = {}
        for dr_row in result:
            day = dr_row.day.isoformat()
            if day not in daily:
                daily[day] = {}
            daily[day][dr_row.status] = dr_row.cnt

        return {"daily_counts": daily, "days": days}
    except ProgrammingError as exc:
        _log.exception(_CODE_DASHBOARD_DAILY_RUN_COUNTS)
        raise HTTPException(
            status_code=http_status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from exc
    except SQLAlchemyError as exc:
        _log.exception(_CODE_DASHBOARD_DAILY_RUN_COUNTS)
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_TEMPORARILY_UNAVAILABLE,
        ) from exc
    except Exception as exc:
        _log.exception("dashboard.daily_run_counts_failed")
        raise HTTPException(
            status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while loading daily run counts.",
        ) from exc
