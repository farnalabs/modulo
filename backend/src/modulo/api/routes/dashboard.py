"""GET /api/v1/dashboard/summary — org-level dashboard widgets."""

import json
import logging
import time as _time
from datetime import UTC, date, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import status as http_status
from sqlalchemy import Date, case, cast, func, select
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.dependencies import get_db_session
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.core.remy.config_service import RemyConfigService
from modulo.db.models.daily_run_count import OrgDailyRunCount
from modulo.db.models.eval_result import EvalResult
from modulo.db.models.feedback_record import FeedbackRecord
from modulo.db.models.hitl_claim import HitlClaim
from modulo.db.models.model_backend import ModelBackend
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.run import Run
from modulo.db.models.team import Team
from modulo.db.rls import set_rls_org
from modulo.settings import get_settings

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/dashboard", tags=["dashboard"])


def _safe_int(value: object, default: int = 0) -> int:
    """Convert *value* to int, returning *default* for None, NaN, or conversion error."""
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value: object, default: float = 0.0) -> float:
    """Convert *value* to float, returning *default* for None, NaN, or conversion error."""
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default

_ACTIVE_RUN_STATUSES = frozenset({"pending", "running", "awaiting_human", "claimed", "waiting_for_lock"})
_TRACKED_STATUSES = ("running", "awaiting_human", "failed", "idle")

_DASHBOARD_CACHE_TTL = 60  # seconds — dashboard summary cached to avoid repeated aggregate queries
_in_memory_cache: dict[str, tuple[float, dict[str, Any]]] = {}


async def _get_cached_dashboard(org_id: str) -> dict[str, Any] | None:
    """Try Redis then in-memory cache."""
    settings = get_settings()
    if settings.redis_url:
        redis: Any = None
        try:
            from redis.asyncio import Redis

            redis = Redis.from_url(settings.redis_url, decode_responses=True)
            key = f"dashboard:summary:{org_id}"
            cached = await redis.get(key)
            if cached:
                cached_data: dict[str, Any] = json.loads(cached)
                return cached_data
        except Exception as exc:
            _log.warning("dashboard.cache_read_failed — %s", exc)
        finally:
            if redis is not None:
                await redis.aclose()
    entry = _in_memory_cache.get(org_id)
    if entry is not None and (_time.monotonic() - entry[0]) < _DASHBOARD_CACHE_TTL:
        return json.loads(json.dumps(entry[1], default=str))
    return None


async def _set_cached_dashboard(org_id: str, data: dict[str, Any]) -> None:
    settings = get_settings()
    if settings.redis_url:
        redis: Any = None
        try:
            from redis.asyncio import Redis

            redis = Redis.from_url(settings.redis_url, decode_responses=True)
            key = f"dashboard:summary:{org_id}"
            await redis.setex(key, _DASHBOARD_CACHE_TTL, json.dumps(data, default=str))
            return
        except Exception as exc:
            _log.warning("dashboard.cache_write_failed — %s", exc)
        finally:
            if redis is not None:
                await redis.aclose()
    _in_memory_cache[org_id] = (_time.monotonic(), data)


@router.get("/summary")
async def dashboard_summary(
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> dict[str, Any]:
    """Org-level dashboard summary with counts, team breakdown, eval pass rate, and 7-day trend."""
    try:
        org_id_str = str(principal.organisation_id)

        cached = await _get_cached_dashboard(org_id_str)
        if cached is not None:
            return cached

        async with session.begin():
            await set_rls_org(session, principal.organisation_id)

            org_id = principal.organisation_id

            # --- Queries that can all run independently (no dependencies between them) ---

            active_pipelines_result = await session.execute(
                select(func.count()).select_from(Pipeline).where(Pipeline.organisation_id == org_id)
            )
            active_pipelines = _safe_int(active_pipelines_result.scalar_one())

            status_count_query = (
                select(
                    Run.status,
                    func.count().label("cnt"),
                )
                .where(Run.organisation_id == org_id)
                .group_by(Run.status)
            )
            status_count_rows = (await session.execute(status_count_query)).all()
            status_counts = {row.status: _safe_int(row.cnt) for row in status_count_rows}

            for tracked_status in _TRACKED_STATUSES:
                status_counts.setdefault(tracked_status, 0)

            total_tracked = sum(status_counts.get(s, 0) for s in _TRACKED_STATUSES)
            active_in_tracked = sum(status_counts.get(s, 0) for s in ("running", "awaiting_human"))
            failed_count = status_counts.get("failed", 0)
            status_counts["idle"] = total_tracked - active_in_tracked - failed_count

            teams_result = await session.execute(select(Team).where(Team.organisation_id == org_id).order_by(Team.name))
            teams = list(teams_result.scalars().all())

            team_run_query = (
                select(
                    Run.owner_team_id,
                    Run.status,
                    func.count().label("cnt"),
                )
                .where(
                    Run.organisation_id == org_id,
                    Run.owner_team_id.is_not(None),
                )
                .group_by(Run.owner_team_id, Run.status)
            )
            team_run_rows = (await session.execute(team_run_query)).all()

            team_pipeline_query = (
                select(
                    Run.owner_team_id,
                    func.count(func.distinct(Run.pipeline_id)).label("pipeline_cnt"),
                )
                .where(
                    Run.organisation_id == org_id,
                    Run.owner_team_id.is_not(None),
                )
                .group_by(Run.owner_team_id)
            )
            team_pipeline_rows = (await session.execute(team_pipeline_query)).all()

            team_run_data: dict[str, dict[str, int]] = {}
            for tr_row in team_run_rows:
                tid = str(tr_row.owner_team_id)
                team_run_data.setdefault(tid, {})[tr_row.status] = _safe_int(tr_row.cnt)

            team_pipeline_data: dict[str, int] = {}
            for tp_row in team_pipeline_rows:
                team_pipeline_data[str(tp_row.owner_team_id)] = int(tp_row.pipeline_cnt)

            team_metrics: list[dict[str, Any]] = []
            for team in teams:
                tid = str(team.id)
                run_data = team_run_data.get(tid, {})
                team_total = sum(run_data.get(s, 0) for s in _TRACKED_STATUSES)
                team_statuses: dict[str, int] = {}
                for tracked_status in _TRACKED_STATUSES:
                    team_statuses[tracked_status] = run_data.get(tracked_status, 0)
                team_active_in_tracked = sum(run_data.get(s, 0) for s in ("running", "awaiting_human"))
                team_failed = run_data.get("failed", 0)
                team_statuses["idle"] = team_total - team_active_in_tracked - team_failed

                team_metrics.append(
                    {
                        "id": tid,
                        "name": team.name,
                        "total_runs": team_total,
                        "active_pipelines": team_pipeline_data.get(tid, 0),
                        "run_counts_by_status": team_statuses,
                    }
                )

            # --- Single merged eval query ---
            eval_totals_query = (
                select(
                    func.count().label("total"),
                    func.sum(case((EvalResult.passed == True, 1), else_=0)).label("passed"),  # noqa: E712
                )
                .select_from(EvalResult)
                .where(EvalResult.organisation_id == org_id)
            )
            eval_totals_row = (await session.execute(eval_totals_query)).one()
            eval_total = int(eval_totals_row.total) if eval_totals_row.total is not None else 0
            eval_passed = int(eval_totals_row.passed) if eval_totals_row.passed is not None else 0

            # --- Superset query: per-team-pipeline eval breakdown; derive per-team and per-pipeline client-side ---
            per_team_pipeline_query = (
                select(
                    Run.owner_team_id,
                    Run.pipeline_id,
                    func.count().label("total"),
                    func.sum(case((EvalResult.passed == True, 1), else_=0)).label("passed"),  # noqa: E712
                )
                .select_from(EvalResult)
                .join(Run, EvalResult.run_id == Run.id)
                .where(
                    EvalResult.organisation_id == org_id,
                    Run.owner_team_id.is_not(None),
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
                team_entry = per_team_eval.setdefault(team_id, {"total_evals": 0, "passed_evals": 0, "pass_rate": 0.0})
                team_entry["total_evals"] += total
                team_entry["passed_evals"] += passed
                team_entry["pass_rate"] = (
                    round(team_entry["passed_evals"] / team_entry["total_evals"] * 100, 1)
                    if team_entry["total_evals"] > 0 else 0.0
                )
                # Derive per-pipeline aggregates
                pipe_entry = per_pipeline.setdefault(pipeline_id, {"total_evals": 0, "passed_evals": 0, "pass_rate": 0.0})
                pipe_entry["total_evals"] += total
                pipe_entry["passed_evals"] += passed
                pipe_entry["pass_rate"] = (
                    round(pipe_entry["passed_evals"] / pipe_entry["total_evals"] * 100, 1)
                    if pipe_entry["total_evals"] > 0 else 0.0
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

            for team_entry in team_metrics:
                if team_eval_data := per_team_eval.get(team_entry["id"]):
                    team_entry["eval_pass_rate"] = team_eval_data

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
                    func.sum(case((EvalResult.passed == True, 1), else_=0)).label("passed"),  # noqa: E712
                )
                .where(
                    EvalResult.organisation_id == org_id,
                    EvalResult.evaluated_at >= seven_days_ago,
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
                .where(Run.organisation_id == org_id)
                .order_by(Run.created_at.desc())
                .limit(10)
            )
            recent_runs_rows = (await session.execute(recent_runs_query)).all()
            recent_runs = [
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

            # ── Config warnings ───────────────────────────────────────────
            config_warnings: list[dict[str, Any]] = []

            try:
                mb_with_creds_result = await session.execute(
                    select(func.count()).select_from(ModelBackend).where(
                        ModelBackend.organisation_id == org_id,
                        ModelBackend.credentials_ciphertext.is_not(None),
                    )
                )
                mb_with_creds = int(mb_with_creds_result.scalar_one())
            except Exception:
                mb_with_creds = 0

            if mb_with_creds == 0:
                config_warnings.append(
                    {
                        "type": "no_model_backends",
                        "severity": "high",
                        "message": "No AI providers configured. Add a model backend with API credentials to run pipelines.",
                        "action_label": "Configure provider",
                        "action_url": "/admin/model-backends",
                    }
                )
            else:
                try:
                    remy_config = await RemyConfigService(session).get_config(org_id)
                    default_provider = remy_config.default_provider
                    provider_creds_result = await session.execute(
                        select(func.count()).select_from(ModelBackend).where(
                            ModelBackend.organisation_id == org_id,
                            ModelBackend.provider == default_provider,
                            ModelBackend.credentials_ciphertext.is_not(None),
                        )
                    )
                    provider_count = int(provider_creds_result.scalar_one())
                    if provider_count == 0:
                        config_warnings.append(
                            {
                                "type": "remy_provider_not_configured",
                                "severity": "high",
                                "message": f"Remy is configured to use {default_provider} but no API key has been set for that provider.",
                                "action_label": f"Configure {default_provider}",
                                "action_url": "/admin/model-backends",
                            }
                        )
                except Exception:
                    _log.warning("dashboard.config_warnings.remy_failed", exc_info=True)

        total_runs = sum(status_counts.values())
        result = {
            "total_runs": total_runs,
            "active_pipelines": active_pipelines,
            "run_counts_by_status": status_counts,
            "teams": team_metrics,
            "eval_pass_rate": eval_pass_rate,
            "trend": trend,
            "recent_runs": recent_runs,
            "config_warnings": config_warnings,
        }

        await _set_cached_dashboard(org_id_str, result)
        return result
    except ProgrammingError:
        raise HTTPException(
            status_code=http_status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        )
    except SQLAlchemyError:
        raise HTTPException(
            status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="A database error occurred.",
        )
    except Exception:
        _log.exception("dashboard.summary_failed")
        raise HTTPException(
            status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while loading the dashboard.",
        )


@router.get("/trends")
async def dashboard_trends(
    days: int = Query(7, ge=1, le=90),
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> dict[str, Any]:
    """Trend data over the specified number of days — run counts, eval pass rate, token spend."""
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)

            org_id = principal.organisation_id
            today = datetime.now(UTC).date()
            start_date = today - timedelta(days=days - 1)

            eval_query = (
                select(
                    cast(EvalResult.evaluated_at, Date).label("eval_date"),
                    func.count().label("total"),
                    func.sum(case((EvalResult.passed == True, 1), else_=0)).label("passed"),  # noqa: E712
                )
                .where(
                    EvalResult.organisation_id == org_id,
                    EvalResult.evaluated_at >= start_date,
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

            # ------------------------------------------------------------------
            # HITL volume / trend tracking (§8.20)
            # ------------------------------------------------------------------

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

            # Build daily hitl series aligned with the trend date range
            hitl_volume: list[dict[str, Any]] = []
            for i in range(days):
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
            correlation: list[dict[str, Any]] = []
            for h in hitl_volume:
                eval_rate = eval_rate_map.get(h["date"])
                correlation.append(
                    {
                        "date": h["date"],
                        "rejection_rate": h["rejection_rate"],
                        "eval_pass_rate": eval_rate,
                    }
                )

            # Feedback-record volume (by date created)
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
            for i in range(days):
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
    except ProgrammingError:
        raise HTTPException(
            status_code=http_status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        )
    except SQLAlchemyError:
        raise HTTPException(
            status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="A database error occurred.",
        )
    except Exception:
        _log.exception("dashboard.trends_failed")
        raise HTTPException(
            status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while loading trends.",
        )


@router.get("/daily-run-counts")
async def daily_run_counts(
    days: int = Query(30, ge=1, le=365),
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
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
    except ProgrammingError:
        raise HTTPException(
            status_code=http_status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        )
    except SQLAlchemyError:
        raise HTTPException(
            status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="A database error occurred.",
        )
    except Exception:
        _log.exception("dashboard.daily_run_counts_failed")
        raise HTTPException(
            status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while loading daily run counts.",
        )
