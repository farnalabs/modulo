"""Admin cost management routes — spend limits, cost reports, export, anomalies, scheduled reports."""

import csv
import io
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.dependencies import get_db_session, require_feature
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.core.cost_controller import get_cost_report
from modulo.db.crud.organisation import get_organisation
from modulo.db.crud.scheduled_report import (
    create_scheduled_report,
    delete_scheduled_report,
    list_scheduled_reports,
)
from modulo.db.crud.spend_anomaly import dismiss_anomaly, list_anomalies
from modulo.db.crud.team import get_team, list_teams
from modulo.db.models.daily_run_count import OrgDailyRunCount
from modulo.db.rls import set_rls_org

router = APIRouter(prefix="/api/v1/admin/costs", tags=["admin", "costs"])


class CostReportRow(BaseModel):
    entity_id: str
    entity_name: str
    total_spend_usd: float
    total_runs: int


class CostReportResponse(BaseModel):
    period: str
    group_by: str
    items: list[CostReportRow]


class SpendLimitResponse(BaseModel):
    organisation_id: str
    org_daily_spend_limit: float | None
    team_limits: list[dict[str, Any]]


class SetSpendLimitRequest(BaseModel):
    daily_spend_limit: float | None = Field(None, ge=0)


def _require_admin(principal: AuthenticatedPrincipal) -> None:
    if principal.org_role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admin users can perform this action",
        )


@router.get("", response_model=CostReportResponse)
async def get_costs(
    group_by: str = Query("team", pattern=r"^(team|org)$"),
    period: str = Query("month", pattern=r"^(day|week|month|year)$"),
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> CostReportResponse:
    _require_admin(current_user)

    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            rows = await get_cost_report(
                session,
                org_id=current_user.organisation_id,
                group_by=group_by,
                period=period,
            )
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None

    return CostReportResponse(
        period=period,
        group_by=group_by,
        items=[CostReportRow(**r) for r in rows],
    )


@router.get("/limits", response_model=SpendLimitResponse)
async def get_spend_limits(
    _: None = require_feature("admin_spend_limits"),
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> SpendLimitResponse:
    _require_admin(current_user)

    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            org = await get_organisation(session, current_user.organisation_id)

            from modulo.db.crud.team import list_teams

            teams_result = await list_teams(session, org_id=current_user.organisation_id, page=1, page_size=1000)
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None

    return SpendLimitResponse(
        organisation_id=str(current_user.organisation_id),
        org_daily_spend_limit=(float(org.daily_spend_limit) if org and org.daily_spend_limit is not None else None),
        team_limits=[
            {
                "team_id": str(t.id),
                "team_name": t.name,
                "daily_spend_limit": (float(t.daily_spend_limit) if t.daily_spend_limit is not None else None),
            }
            for t in teams_result.items
        ],
    )


@router.put("/limits/org", response_model=dict[str, Any])
async def set_org_spend_limit(
    body: SetSpendLimitRequest,
    _: None = require_feature("admin_spend_limits"),
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    _require_admin(current_user)

    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            org = await get_organisation(session, current_user.organisation_id)
            if org is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organisation not found")
            org.daily_spend_limit = Decimal(str(body.daily_spend_limit)) if body.daily_spend_limit is not None else None
            await session.flush()
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None

    return {
        "organisation_id": str(org.id),
        "daily_spend_limit": body.daily_spend_limit,
    }


@router.put("/limits/teams/{team_id}", response_model=dict[str, Any])
async def set_team_spend_limit(
    team_id: uuid.UUID,
    body: SetSpendLimitRequest,
    _: None = require_feature("admin_spend_limits"),
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    _require_admin(current_user)

    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            team = await get_team(session, team_id)
            if team is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Team not found")
            team.daily_spend_limit = Decimal(str(body.daily_spend_limit)) if body.daily_spend_limit is not None else None
            await session.flush()
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None

    return {
        "team_id": team_id,
        "daily_spend_limit": body.daily_spend_limit,
    }


class CostControlsResponse(BaseModel):
    teams: list[dict[str, object]]
    budget: float | None = None
    alert_thresholds: list[float] = []
    circuit_breaker_enabled: bool = False
    currency: str = "USD"
    billing_period: str = "monthly"


class UpdateCostControlsRequest(BaseModel):
    budget: float | None = None
    alert_thresholds: list[float] | None = None
    circuit_breaker_enabled: bool | None = None
    currency: str | None = None
    billing_period: str | None = None


@router.get("/controls", response_model=CostControlsResponse)
async def get_cost_controls(
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> CostControlsResponse:
    _require_admin(current_user)
    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            teams_result = await list_teams(session, org_id=current_user.organisation_id, page=1, page_size=1000)
            org = await get_organisation(session, current_user.organisation_id)
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None


    return CostControlsResponse(
        teams=[
            {
                "id": str(t.id),
                "name": t.name,
                "daily_limit_usd": float(t.daily_spend_limit) if t.daily_spend_limit is not None else None,
            }
            for t in teams_result.items
        ],
        budget=float(org.daily_spend_limit) if org and org.daily_spend_limit is not None else None,
    )


@router.put("/controls", response_model=CostControlsResponse)
async def update_cost_controls(
    body: UpdateCostControlsRequest,
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> CostControlsResponse:
    _require_admin(current_user)
    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            if body.budget is not None:
                org = await get_organisation(session, current_user.organisation_id)
                if org is None:
                    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organisation not found")
                from decimal import Decimal
                org.daily_spend_limit = Decimal(str(body.budget))
                await session.flush()

            teams_result = await list_teams(session, org_id=current_user.organisation_id, page=1, page_size=1000)
            org = await get_organisation(session, current_user.organisation_id)
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None

    return CostControlsResponse(
        teams=[
            {
                "id": str(t.id),
                "name": t.name,
                "daily_limit_usd": float(t.daily_spend_limit) if t.daily_spend_limit is not None else None,
            }
            for t in teams_result.items
        ],
        budget=float(org.daily_spend_limit) if org and org.daily_spend_limit is not None else None,
    )


# ── Export ────────────────────────────────────────────────────────────────────


@router.get("/export")
async def export_costs(
    period: str = Query("this_month", pattern=r"^(this_month|last_month|7d|30d|90d)$"),
    group_by: str = Query("team", pattern=r"^(team|pipeline|model)$"),
    format: str = Query("csv", pattern=r"^(csv)$"),
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    _require_admin(current_user)

    period_map: dict[str, str] = {
        "this_month": "month",
        "last_month": "month",
        "7d": "week",
        "30d": "month",
        "90d": "year",
    }

    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            rows = await get_cost_report(
                session,
                org_id=current_user.organisation_id,
                group_by=group_by if group_by != "model" else "team",
                period=period_map.get(period, "month"),
            )
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["entity_id", "entity_name", "total_spend_usd", "total_runs"])
    for r in rows:
        writer.writerow([r["entity_id"], r["entity_name"], r["total_spend_usd"], r["total_runs"]])
    csv_content = output.getvalue()

    return Response(
        content=csv_content,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="costs-export-{period}.csv"'},
    )


# ── Scheduled Reports ────────────────────────────────────────────────────────


class CreateReportRequest(BaseModel):
    period: str = Field(pattern=r"^(daily|weekly|monthly)$")
    group_by: str = Field(pattern=r"^(team|pipeline|model)$")
    format: str = Field(default="csv", pattern=r"^(csv|json)$")
    recipients: list[str] = Field(min_length=1)
    schedule_type: str = Field(default="one_time", pattern=r"^(one_time|recurring)$")


class ReportResponse(BaseModel):
    id: str
    period: str
    group_by: str
    format: str
    recipients: list[str]
    schedule_type: str
    created_at: str


@router.post("/reports", response_model=ReportResponse, status_code=201)
async def create_report(
    body: CreateReportRequest,
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> ReportResponse:
    _require_admin(current_user)

    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            report = await create_scheduled_report(
                session,
                organisation_id=current_user.organisation_id,
                period=body.period,
                group_by=body.group_by,
                format=body.format,
                recipients=body.recipients,
                schedule_type=body.schedule_type,
                account_id=current_user.account_id,
            )
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None

    return ReportResponse(
        id=str(report.id),
        period=report.period,
        group_by=report.group_by,
        format=report.format,
        recipients=list(report.recipients) if isinstance(report.recipients, list) else [],
        schedule_type=report.schedule_type,
        created_at=report.created_at.isoformat(),
    )


@router.get("/reports", response_model=list[ReportResponse])
async def list_reports(
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> list[ReportResponse]:
    _require_admin(current_user)

    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            reports = await list_scheduled_reports(
                session,
                organisation_id=current_user.organisation_id,
            )
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None

    return [
        ReportResponse(
            id=str(r.id),
            period=r.period,
            group_by=r.group_by,
            format=r.format,
            recipients=list(r.recipients) if isinstance(r.recipients, list) else [],
            schedule_type=r.schedule_type,
            created_at=r.created_at.isoformat(),
        )
        for r in reports
    ]


@router.delete("/reports/{report_id}", status_code=204)
async def delete_report(
    report_id: uuid.UUID,
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> None:
    _require_admin(current_user)

    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            deleted = await delete_scheduled_report(
                session,
                report_id=report_id,
                organisation_id=current_user.organisation_id,
            )
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None

    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Report not found")


# ── Anomalies ──────────────────────────────────────────────────────────────────


class AnomalyResponse(BaseModel):
    id: str
    anomaly_date: str
    pipeline_id: str | None
    amount: float
    baseline: float
    percent_above: float
    dismissed: bool


@router.get("/anomalies", response_model=list[AnomalyResponse])
async def get_anomalies(
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> list[AnomalyResponse]:
    _require_admin(current_user)

    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)

            # Detect anomalies: daily org spend > 2x rolling 7-day avg
            today = datetime.now(UTC).date()
            lookback = today - timedelta(days=30)

            counts_q = (
                select(
                    OrgDailyRunCount.run_date,
                    func.sum(OrgDailyRunCount.total_spend_usd).label("daily_spend"),
                )
                .where(
                    OrgDailyRunCount.organisation_id == current_user.organisation_id,
                    OrgDailyRunCount.run_date >= lookback,
                    OrgDailyRunCount.team_id.is_(None),
                )
                .group_by(OrgDailyRunCount.run_date)
                .order_by(OrgDailyRunCount.run_date)
            )

            counts_result = await session.execute(counts_q)
            daily_spends: list[tuple[object, object]] = [(r.run_date, r.daily_spend) for r in counts_result.all()]

            anomalies: list[dict[str, Any]] = []
            for i, (run_date, spend) in enumerate(daily_spends):
                if i < 7:
                    continue
                window = [s for _, s in daily_spends[max(0, i - 7) : i] if s is not None]
                if not window:
                    continue
                avg = sum(float(str(w)) for w in window) / len(window)
                if avg == 0:
                    continue
                spend_val = float(str(spend)) if spend else 0.0
                ratio = spend_val / avg
                if ratio > 2.0:
                    anomalies.append(
                        {
                            "id": "",
                            "anomaly_date": str(run_date),
                            "pipeline_id": None,
                            "amount": spend_val,
                            "baseline": avg,
                            "percent_above": round((ratio - 1.0) * 100, 2),
                            "dismissed": False,
                        }
                    )

            # Also return any previously stored anomalies
            stored = await list_anomalies(session, organisation_id=current_user.organisation_id, dismissed=False)
            stored_dict: dict[str, Any] = {}
            for a in stored:
                key = str(a.anomaly_date)
                if key not in stored_dict:
                    stored_dict[key] = {
                        "id": str(a.id),
                        "anomaly_date": str(a.anomaly_date),
                        "pipeline_id": str(a.pipeline_id) if a.pipeline_id else None,
                        "amount": float(a.amount),
                        "baseline": float(a.baseline),
                        "percent_above": float(a.percent_above),
                        "dismissed": a.dismissed,
                    }

            # Merge: use stored dismissed status, and include stored anomalies
            seen_dates: set[str] = set()
            for a in anomalies:  # type: ignore[assignment]
                key = a["anomaly_date"]  # type: ignore[index]
                seen_dates.add(key)
                if key in stored_dict:
                    a["dismissed"] = stored_dict[key]["dismissed"]  # type: ignore[index]

            for key, sa in stored_dict.items():
                if key not in seen_dates:
                    anomalies.append(sa)

            return [AnomalyResponse(**a) for a in anomalies]
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None


@router.get("/anomalies/dismiss/{anomaly_id}", status_code=204)
async def dismiss_anomaly_endpoint(
    anomaly_id: uuid.UUID,
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> None:
    _require_admin(current_user)

    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            dismissed = await dismiss_anomaly(
                session,
                anomaly_id=anomaly_id,
                organisation_id=current_user.organisation_id,
            )
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None

    if not dismissed:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Anomaly not found")
