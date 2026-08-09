"""GET /api/v1/analytics/query + /export — typed-params analytics over run_daily_facts (ADR 020).

The backend is the SOLE bucketing authority: day/hour/ISO-week bucketing and
zero-fill happen in the shared service (``modulo.core.analytics.service``),
never on the client. Tenant isolation relies on the EXPLICIT
``organisation_id = :org`` predicate injected by the SQL builder (modulo_app is
BYPASSRLS on Postgres and the ORM tenant filter is NOT registered there) — RLS
via ``set_rls_org`` is defense-in-depth, not the control. Every request sets a
bounded ``statement_timeout`` so a runaway date range degrades to a clean 503
instead of hogging a pooled connection.

The route is a thin adapter over the service: it maps the service's typed
``AnalyticsError`` exceptions to HTTP status codes and passes the query params
through unchanged. The ``query_analytics`` MCP tool shares the same service.
"""

from __future__ import annotations

import csv
import io
import logging
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import async_sessionmaker
from starlette import status as http_status

from modulo.api.dependencies import get_or_create_engine, require_feature, require_permission
from modulo.auth.jwt import TenantPrincipal
from modulo.core.analytics.builder import (
    AnalyticsDimension,
    AnalyticsGroupBy,
    AnalyticsStatus,
    AnalyticsTriggerType,
)
from modulo.core.analytics.service import (
    EXPORT_COLUMN_NAMES,
    AnalyticsDatabaseError,
    AnalyticsMigrationRequiredError,
    AnalyticsParams,
    AnalyticsQueryTimeoutError,
    AnalyticsRateLimitedError,
    AnalyticsValidationError,
    export_facts,
    run_analytics_query,
)
from modulo.settings import Settings, get_settings

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/analytics", tags=["analytics"])

# Export pagination bounds (FAR-102, Part D).
_EXPORT_DEFAULT_LIMIT = 500
_EXPORT_MAX_LIMIT = 5000


class AnalyticsBucket(BaseModel):
    date: str
    key: str | None = None
    count: int = 0
    total_cost_usd: float | None = None
    total_tokens: int | None = None
    avg_duration_ms: float | None = None
    success_rate: float | None = None
    failure_count: int = 0
    stall_count: int = 0
    avg_queue_wait_ms: float | None = None
    avg_final_idle_ms: float | None = None
    avg_output_bytes: float | None = None


class AnalyticsResponse(BaseModel):
    group_by: str
    dimension: str | None = None
    date_from: str | None = None
    date_to: str | None = None
    buckets: list[AnalyticsBucket]


class AnalyticsExportItem(BaseModel):
    """One raw fact row — all fact columns, serialised to JSON-safe values."""

    run_id: str
    run_date: str
    team_id: str | None = None
    team_name: str | None = None
    pipeline_id: str | None = None
    pipeline_name: str | None = None
    folder_id: str | None = None
    trigger_type: str
    status: str
    total_cost_usd: float | None = None
    total_tokens: int | None = None
    duration_ms: int | None = None
    error_code: str | None = None
    claim_count: int | None = None
    queue_wait_ms: int | None = None
    final_idle_ms: int | None = None
    cancellation_requested: bool | None = None
    dispatcher: str | None = None
    node_count: int | None = None
    sandbox_agent_node_count: int | None = None
    max_node_timeout_seconds: int | None = None
    parent_run_id: str | None = None
    snapshot_id: str | None = None
    run_number: int | None = None
    output_bytes: int | None = None
    rate_limited: bool | None = None
    created_at: str


class AnalyticsExportResponse(BaseModel):
    items: list[AnalyticsExportItem]
    total: int
    offset: int
    limit: int


def _analytics_session_factory(settings: Settings) -> async_sessionmaker[Any]:
    """Dedicated sessionmaker over the EXISTING shared engine (autobegin=False)."""
    return async_sessionmaker(get_or_create_engine(settings), expire_on_commit=False, autobegin=False)


def _build_params(
    *,
    group_by: AnalyticsGroupBy,
    auto_granularity: bool,
    dimension: AnalyticsDimension | None,
    trigger_type: AnalyticsTriggerType | None,
    status: AnalyticsStatus | None,
    pipeline_ids: tuple[uuid.UUID, ...],
    error_code: str | None,
    folder_id: uuid.UUID | None,
    date_from: Any,
    date_to: Any,
    limit: int,
) -> AnalyticsParams:
    return AnalyticsParams(
        group_by=group_by,
        auto_granularity=auto_granularity,
        dimension=dimension,
        trigger_type=trigger_type,
        status=status,
        pipeline_ids=pipeline_ids,
        error_code=error_code,
        folder_id=folder_id,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
    )


def _require_org(principal: TenantPrincipal) -> uuid.UUID:
    org_id = principal.organisation_id
    if org_id is None:
        raise HTTPException(
            status_code=http_status.HTTP_403_FORBIDDEN,
            detail="Analytics requires an organisation context",
        )
    return org_id


def _map_service_error(exc: Exception) -> HTTPException:
    """Map a typed service error to the REST HTTP response."""
    if isinstance(exc, AnalyticsRateLimitedError):
        return HTTPException(
            status_code=http_status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded",
        )
    if isinstance(exc, AnalyticsValidationError):
        return HTTPException(status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY, detail=exc.detail)
    if isinstance(exc, AnalyticsQueryTimeoutError):
        return HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        )
    if isinstance(exc, AnalyticsMigrationRequiredError):
        return HTTPException(
            status_code=http_status.HTTP_501_NOT_IMPLEMENTED,
            detail=str(exc),
        )
    if isinstance(exc, AnalyticsDatabaseError):
        return HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        )
    _log.exception("analytics.route.unexpected_error")
    return HTTPException(
        status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="An unexpected error occurred.",
    )


@router.get("/query", response_model=AnalyticsResponse)
async def analytics_query(
    group_by: AnalyticsGroupBy = Query(AnalyticsGroupBy.DAY),
    auto_granularity: bool = Query(False),
    dimension: AnalyticsDimension | None = Query(None),
    trigger_type: AnalyticsTriggerType | None = Query(None),
    status: AnalyticsStatus | None = Query(None),
    pipeline_id: list[uuid.UUID] | None = Query(None),
    error_code: str | None = Query(None),
    folder_id: uuid.UUID | None = Query(None),
    date_from: datetime | None = Query(None),
    date_to: datetime | None = Query(None),
    limit: int = Query(1000, ge=1, le=1000),
    settings: Settings = Depends(get_settings),
    principal: TenantPrincipal = require_permission("analytics.query"),
    _: object = require_feature("analytics_page"),
) -> AnalyticsResponse:
    """Bucketed run-facts series over the requested range, grouped hour/day/ISO-week.

    ``pipeline_id`` may be repeated for "A vs B" comparisons in a single
    request. ``error_code`` filters to a specific failure code and doubles as a
    group-by dimension (``dimension=error_code``). ``date_from``/``date_to``
    accept bare dates ("2026-08-06", parsed as midnight UTC) or ISO datetimes
    ("2026-08-06T14:00:00Z"). ``auto_granularity=true`` overrides ``group_by``
    from the effective range span (hour ≤3d, day ≤90d, week otherwise).
    """
    org_id = _require_org(principal)
    params = _build_params(
        group_by=group_by,
        auto_granularity=auto_granularity,
        dimension=dimension,
        trigger_type=trigger_type,
        status=status,
        pipeline_ids=tuple(pipeline_id or ()),
        error_code=error_code,
        folder_id=folder_id,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
    )
    try:
        result = await run_analytics_query(
            org_id=org_id,
            params=params,
            factory=_analytics_session_factory(settings),
            settings=settings,
            account_id=principal.account_id,
            org_role=principal.org_role,
        )
    except Exception as exc:
        if isinstance(exc, HTTPException):
            raise
        raise _map_service_error(exc) from None
    return AnalyticsResponse(**result)


@router.get("/export", response_model=AnalyticsExportResponse)
async def analytics_export(
    format: str = Query("json", pattern="^(json|csv)$"),
    offset: int = Query(0, ge=0),
    limit: int = Query(_EXPORT_DEFAULT_LIMIT, ge=1, le=_EXPORT_MAX_LIMIT),
    dimension: AnalyticsDimension | None = Query(None),
    trigger_type: AnalyticsTriggerType | None = Query(None),
    status: AnalyticsStatus | None = Query(None),
    pipeline_id: list[uuid.UUID] | None = Query(None),
    error_code: str | None = Query(None),
    folder_id: uuid.UUID | None = Query(None),
    date_from: datetime | None = Query(None),
    date_to: datetime | None = Query(None),
    settings: Settings = Depends(get_settings),
    principal: TenantPrincipal = require_permission("analytics.query"),
    _: object = require_feature("analytics_page"),
) -> Response:
    """Raw fact rows (no bucketing) filtered by the same typed params.

    Paginated via ``offset``/``limit`` (default 500, max 5000), ordered by
    ``run_date``/``created_at``. ``format=json`` (default) returns structured
    rows; ``format=csv`` returns a Content-Disposition attachment with one row
    per fact and one column per fact field. ``dimension`` is accepted for
    surface parity but ignored — export has no bucketing.
    """
    org_id = _require_org(principal)
    params = _build_params(
        group_by=AnalyticsGroupBy.DAY,
        auto_granularity=False,
        dimension=dimension,
        trigger_type=trigger_type,
        status=status,
        pipeline_ids=tuple(pipeline_id or ()),
        error_code=error_code,
        folder_id=folder_id,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
    )
    try:
        result = await export_facts(
            org_id=org_id,
            params=params,
            factory=_analytics_session_factory(settings),
            settings=settings,
            account_id=principal.account_id,
            org_role=principal.org_role,
            offset=offset,
            limit=limit,
        )
    except Exception as exc:
        if isinstance(exc, HTTPException):
            raise
        raise _map_service_error(exc) from None

    if format == "csv":
        return _csv_response(result)
    return Response(
        content=AnalyticsExportResponse(**result).model_dump_json(),
        media_type="application/json",
    )


def _csv_response(result: dict[str, Any]) -> Response:
    """Render an export result as a CSV attachment with a sanitized filename."""
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(EXPORT_COLUMN_NAMES), extrasaction="ignore")
    writer.writeheader()
    for item in result["items"]:
        writer.writerow(item)
    filename = "analytics-export.csv"
    return Response(
        content=buffer.getvalue(),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )
