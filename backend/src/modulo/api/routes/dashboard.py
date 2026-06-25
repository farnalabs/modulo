"""GET /api/v1/dashboard/summary — org-level dashboard widgets."""

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.dependencies import get_db_session
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.run import Run
from modulo.db.rls import set_rls_org

router = APIRouter(prefix="/api/v1/dashboard", tags=["dashboard"])

_ACTIVE_RUN_STATUSES = frozenset(
    {"pending", "running", "awaiting_human", "claimed", "waiting_for_lock"}
)
_TRACKED_STATUSES = ("running", "awaiting_human", "failed", "idle")


@router.get("/summary")
async def dashboard_summary(
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> dict[str, Any]:
    """Org-level dashboard summary with counts and status breakdown."""
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)

        total_runs_result = await session.execute(
            select(func.count()).select_from(Run)
        )
        total_runs = int(total_runs_result.scalar_one())

        active_pipelines_result = await session.execute(
            select(func.count()).select_from(Pipeline)
        )
        active_pipelines = int(active_pipelines_result.scalar_one())

        status_counts: dict[str, int] = {}
        for status in _TRACKED_STATUSES:
            count_result = await session.execute(
                select(func.count()).select_from(Run).where(Run.status == status)
            )
            status_counts[status] = int(count_result.scalar_one())

        idle_result = await session.execute(
            select(func.count()).select_from(Run).where(
                Run.status.not_in(_ACTIVE_RUN_STATUSES)
            )
        )
        status_counts["idle"] = int(idle_result.scalar_one())

    return {
        "total_runs": total_runs,
        "active_pipelines": active_pipelines,
        "run_counts_by_status": status_counts,
    }
