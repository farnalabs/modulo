"""GET /api/v1/dashboard/summary — org-level dashboard widgets."""

from datetime import UTC, date, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import Date, case, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.dependencies import get_db_session
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.db.models.daily_run_count import OrgDailyRunCount
from modulo.db.models.eval_result import EvalResult
from modulo.db.models.feedback_record import FeedbackRecord
from modulo.db.models.hitl_claim import HitlClaim
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.run import Run
from modulo.db.models.team import Team
from modulo.db.rls import set_rls_org

router = APIRouter(prefix="/api/v1/dashboard", tags=["dashboard"])

_ACTIVE_RUN_STATUSES = frozenset({"pending", "running", "awaiting_human", "claimed", "waiting_for_lock"})
_TRACKED_STATUSES = ("running", "awaiting_human", "failed", "idle")


@router.get("/summary")
async def dashboard_summary(
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> dict[str, Any]:
    """Org-level dashboard summary with counts, team breakdown, eval pass rate, and 7-day trend."""
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)

        org_id = principal.organisation_id

        total_runs_result = await session.execute(
            select(func.count()).select_from(Run).where(Run.organisation_id == org_id)
        )
        total_runs = int(total_runs_result.scalar_one())

        active_pipelines_result = await session.execute(
            select(func.count()).select_from(Pipeline).where(Pipeline.organisation_id == org_id)
        )
        active_pipelines = int(active_pipelines_result.scalar_one())

        status_count_query = (
            select(
                Run.status,
                func.count().label("cnt"),
            )
            .where(Run.organisation_id == org_id)
            .group_by(Run.status)
        )
        status_count_rows = (await session.execute(status_count_query)).all()
        status_counts = {row.status: int(row.cnt) for row in status_count_rows}

        for status in _TRACKED_STATUSES:
            status_counts.setdefault(status, 0)

        active_count = sum(status_counts.get(s, 0) for s in _ACTIVE_RUN_STATUSES if s != "idle")
        status_counts["idle"] = total_runs - active_count

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
        for row in team_run_rows:
            tid = str(row.owner_team_id)
            team_run_data.setdefault(tid, {})[row.status] = int(row.cnt)

        team_pipeline_data: dict[str, int] = {}
        for row in team_pipeline_rows:
            team_pipeline_data[str(row.owner_team_id)] = int(row.pipeline_cnt)

        team_metrics: list[dict[str, Any]] = []
        for team in teams:
            tid = str(team.id)
            run_data = team_run_data.get(tid, {})
            team_total = sum(run_data.values())
            team_statuses: dict[str, int] = {}
            for status in _TRACKED_STATUSES:
                team_statuses[status] = run_data.get(status, 0)
            team_active = sum(run_data.get(s, 0) for s in _ACTIVE_RUN_STATUSES if s != "idle")
            team_statuses["idle"] = team_total - team_active

            team_metrics.append(
                {
                    "id": tid,
                    "name": team.name,
                    "total_runs": team_total,
                    "active_pipelines": team_pipeline_data.get(tid, 0),
                    "run_counts_by_status": team_statuses,
                }
            )

        eval_total_result = await session.execute(
            select(func.count()).select_from(EvalResult).where(EvalResult.organisation_id == org_id)
        )
        eval_total = int(eval_total_result.scalar_one())

        eval_passed_result = await session.execute(
            select(func.count())
            .select_from(EvalResult)
            .where(
                EvalResult.organisation_id == org_id,
                EvalResult.passed == True,  # noqa: E712
            )
        )
        eval_passed = int(eval_passed_result.scalar_one())

        per_team_eval_query = (
            select(
                Run.owner_team_id,
                func.count().label("total"),
                func.sum(case((EvalResult.passed == True, 1), else_=0)).label("passed"),  # noqa: E712
            )
            .select_from(EvalResult)
            .join(Run, EvalResult.run_id == Run.id)
            .where(
                EvalResult.organisation_id == org_id,
                Run.owner_team_id.is_not(None),
            )
            .group_by(Run.owner_team_id)
        )
        per_team_eval_rows = (await session.execute(per_team_eval_query)).all()
        per_team_eval: dict[str, dict[str, Any]] = {}
        for row in per_team_eval_rows:
            total = int(row.total)
            passed = int(row.passed)
            per_team_eval[str(row.owner_team_id)] = {
                "total_evals": total,
                "passed_evals": passed,
                "pass_rate": round(passed / total * 100, 1) if total > 0 else 0.0,
            }

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
        for row in per_team_pipeline_rows:
            team_id = str(row.owner_team_id)
            pipeline_id = str(row.pipeline_id)
            total = int(row.total)
            passed = int(row.passed)
            per_team_pipeline.setdefault(team_id, {})[pipeline_id] = {
                "total_evals": total,
                "passed_evals": passed,
                "pass_rate": round(passed / total * 100, 1) if total > 0 else 0.0,
            }

        eval_pass_rate: dict[str, Any] | None = None
        if eval_total > 0:
            per_pipeline_query = (
                select(
                    Run.pipeline_id,
                    func.count().label("total"),
                    func.sum(case((EvalResult.passed == True, 1), else_=0)).label("passed"),  # noqa: E712
                )
                .select_from(EvalResult)
                .join(Run, EvalResult.run_id == Run.id)
                .where(EvalResult.organisation_id == org_id)
                .group_by(Run.pipeline_id)
            )
            per_pipeline_rows = (await session.execute(per_pipeline_query)).all()
            per_pipeline: dict[str, dict[str, Any]] = {}
            for row in per_pipeline_rows:
                per_pipeline[str(row.pipeline_id)] = {
                    "total_evals": int(row.total),
                    "passed_evals": int(row.passed),
                    "pass_rate": round(int(row.passed) / int(row.total) * 100, 1) if int(row.total) > 0 else 0.0,
                }

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
        for row in daily_rows:
            daily_map[row.run_date] = (
                int(row.run_count) if row.run_count else 0,
                float(row.total_spend) if row.total_spend else 0.0,
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
        for row in daily_eval_rows:
            total = int(row.total)
            passed = int(row.passed)
            daily_eval_map[row.eval_date] = round(passed / total * 100, 1) if total > 0 else None

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
                Pipeline.name.label("pipeline_name"),
                Run.status,
                Run.created_at,
                Run.trigger_type,
            )
            .join(Pipeline, Run.pipeline_id == Pipeline.id)
            .order_by(Run.created_at.desc())
            .limit(10)
        )
        recent_runs_rows = (await session.execute(recent_runs_query)).all()
        recent_runs = [
            {
                "id": str(row.id),
                "pipeline_name": row.pipeline_name,
                "status": row.status,
                "created_at": row.created_at.isoformat(),
                "trigger_type": row.trigger_type,
            }
            for row in recent_runs_rows
        ]

    return {
        "total_runs": total_runs,
        "active_pipelines": active_pipelines,
        "run_counts_by_status": status_counts,
        "teams": team_metrics,
        "eval_pass_rate": eval_pass_rate,
        "trend": trend,
        "recent_runs": recent_runs,
    }


@router.get("/trends")
async def dashboard_trends(
    days: int = Query(7, ge=1, le=90),
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> dict[str, Any]:
    """Trend data over the specified number of days — run counts, eval pass rate, token spend."""
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


@router.get("/daily-run-counts")
async def daily_run_counts(
    days: int = Query(30, ge=1, le=365),
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> dict[str, Any]:
    """Return daily run counts for the last N days, grouped by status."""
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)

        cutoff = datetime.now(UTC) - timedelta(days=days)

        result = await session.execute(
            select(
                func.date_trunc("day", Run.created_at).label("day"),
                Run.status,
                func.count().label("count"),
            )
            .where(
                Run.organisation_id == principal.organisation_id,
                Run.created_at >= cutoff,
            )
            .group_by("day", Run.status)
            .order_by("day")
        )

    daily: dict[str, dict[str, int]] = {}
    for row in result:
        day = row.day.isoformat()
        if day not in daily:
            daily[day] = {}
        daily[day][row.status] = row.count

    return {"daily_counts": daily, "days": days}
