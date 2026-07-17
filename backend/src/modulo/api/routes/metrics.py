"""Web Vitals and performance metrics ingestion."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func as sa_func
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.dependencies import get_db_session
from modulo.auth.dependencies import get_current_tenant_user
from modulo.auth.jwt import TenantPrincipal
from modulo.db.models.web_vital_event import WebVitalEvent
from modulo.db.rls import set_rls_org, set_rls_user_context

_log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/metrics",
    tags=["metrics"],
)


class WebVitalBatchItem(BaseModel):
    metric_name: str = Field(..., max_length=50)
    metric_value: float = Field(..., ge=0)
    metric_rating: str | None = Field(None, max_length=20)
    route_path: str | None = Field(None, max_length=500)
    page_url: str | None = Field(None, max_length=2000)
    navigation_type: str | None = Field(None, max_length=50)


class WebVitalBatchRequest(BaseModel):
    events: list[WebVitalBatchItem]


class WebVitalSummaryItem(BaseModel):
    metric_name: str
    avg_value: float
    min_value: float
    max_value: float
    count: int
    good_pct: float | None


class WebVitalTimeSeriesPoint(BaseModel):
    date: str
    metric_name: str
    avg_value: float
    count: int


@router.post("/web-vitals", status_code=status.HTTP_204_NO_CONTENT)
async def ingest_web_vitals(
    req: WebVitalBatchRequest,
    current_user: TenantPrincipal = Depends(get_current_tenant_user),
    session: AsyncSession = Depends(get_db_session),
) -> None:
    """Ingest a batch of Web Vitals measurements from the frontend."""
    if not req.events:
        return

    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            await set_rls_user_context(session, current_user.account_id, current_user.org_role)

            now = datetime.utcnow()
            for event in req.events:
                wv = WebVitalEvent(
                    organisation_id=current_user.organisation_id,
                    metric_name=event.metric_name,
                    metric_value=event.metric_value,
                    metric_rating=event.metric_rating,
                    route_path=event.route_path,
                    page_url=event.page_url,
                    navigation_type=event.navigation_type,
                    recorded_at=now,
                )
                session.add(wv)
    except SQLAlchemyError:
        _log.exception("Failed to ingest web vitals")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database temporarily unavailable.",
        ) from None


@router.get("/web-vitals/summary")
async def get_web_vitals_summary(
    days: int = Query(7, ge=1, le=90),
    current_user: TenantPrincipal = Depends(get_current_tenant_user),
    session: AsyncSession = Depends(get_db_session),
) -> list[WebVitalSummaryItem]:
    """Get summary statistics for web vitals over the given period."""
    cutoff = datetime.utcnow() - timedelta(days=days)

    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            await set_rls_user_context(session, current_user.account_id, current_user.org_role)

            stmt = (
                select(
                    WebVitalEvent.metric_name,
                    sa_func.avg(WebVitalEvent.metric_value),
                    sa_func.min(WebVitalEvent.metric_value),
                    sa_func.max(WebVitalEvent.metric_value),
                    sa_func.count(WebVitalEvent.id),
                )
                .where(WebVitalEvent.recorded_at >= cutoff)
                .group_by(WebVitalEvent.metric_name)
            )
            result = await session.execute(stmt)
            rows = result.all()

            summaries: list[WebVitalSummaryItem] = []
            for row in rows:
                name = row[0]
                total = row[4]
                good_stmt = (
                    select(sa_func.count(WebVitalEvent.id))
                    .where(WebVitalEvent.metric_name == name)
                    .where(WebVitalEvent.recorded_at >= cutoff)
                    .where(WebVitalEvent.metric_rating == "good")
                )
                good_result = await session.execute(good_stmt)
                good_count = good_result.scalar() or 0

                summaries.append(
                    WebVitalSummaryItem(
                        metric_name=name,
                        avg_value=round(float(row[1] or 0), 2),
                        min_value=round(float(row[2] or 0), 2),
                        max_value=round(float(row[3] or 0), 2),
                        count=total,
                        good_pct=round(good_count / total * 100, 1) if total > 0 else None,
                    )
                )

            return summaries
    except SQLAlchemyError:
        _log.exception("Failed to fetch web vitals summary")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database temporarily unavailable.",
        ) from None


@router.get("/web-vitals/timeseries")
async def get_web_vitals_timeseries(
    metric_name: str = Query(..., max_length=50),
    days: int = Query(7, ge=1, le=90),
    current_user: TenantPrincipal = Depends(get_current_tenant_user),
    session: AsyncSession = Depends(get_db_session),
) -> list[WebVitalTimeSeriesPoint]:
    """Get daily-averaged time series for a specific metric."""
    cutoff = datetime.utcnow() - timedelta(days=days)

    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            await set_rls_user_context(session, current_user.account_id, current_user.org_role)

            stmt = (
                select(
                    sa_func.date(WebVitalEvent.recorded_at),
                    sa_func.avg(WebVitalEvent.metric_value),
                    sa_func.count(WebVitalEvent.id),
                )
                .where(WebVitalEvent.metric_name == metric_name)
                .where(WebVitalEvent.recorded_at >= cutoff)
                .group_by(sa_func.date(WebVitalEvent.recorded_at))
                .order_by(sa_func.date(WebVitalEvent.recorded_at))
            )
            result = await session.execute(stmt)

            return [
                WebVitalTimeSeriesPoint(
                    date=str(row[0]),
                    metric_name=metric_name,
                    avg_value=round(float(row[1] or 0), 2),
                    count=row[2],
                )
                for row in result.all()
            ]
    except SQLAlchemyError:
        _log.exception("Failed to fetch web vitals timeseries")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database temporarily unavailable.",
        ) from None
