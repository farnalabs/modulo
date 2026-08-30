"""GET /api/v1/analytics/query + /export — typed-params analytics over run_daily_facts (ADR 020).

The backend is the SOLE bucketing authority: day/hour/ISO-week bucketing and
zero-fill happen in the shared service (``modulo.core.analytics.service``),
never on the client. Tenant isolation relies on the EXPLICIT
``organisation_id = :org`` predicate injected by the SQL builder (modulo_app is
NOBYPASSRLS on Postgres and the ORM tenant filter is NOT registered there) — RLS
via ``set_rls_org`` is a further defense-in-depth, and the explicit predicate is
the control on the analytics writes. Every request sets a
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

from modulo.api.constants import MSG_UNEXPECTED_ERROR
from modulo.api.dependencies import get_or_create_engine, require_feature, require_permission
from modulo.auth.jwt import TenantPrincipal
from modulo.core.analytics.builder import (
    AnalyticsDimension,
    AnalyticsGroupBy,
    AnalyticsStatus,
    AnalyticsTriggerType,
)
from modulo.core.analytics.guardrails import run_guardrail_scorecard
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
    run_concurrency_query,
)
from modulo.settings import Settings, get_settings

_CODE_ANALYTICS_QUERY = "analytics.query"


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
    # FAR-200 facts-freshness indicator: hours since the org's newest day with a
    # TERMINAL-status fact row (None when the org has no terminal facts yet),
    # and whether that lags > ~36h. The frontend surfaces a "stale data" notice
    # when ``facts_stale`` is true.
    facts_freshness_hours: float | None = None
    facts_stale: bool = False
    buckets: list[AnalyticsBucket]


class ConcurrencyBucket(BaseModel):
    """One slot-utilization bucket — max/avg concurrent active + queued runs.

    ``key`` is always ``None`` (concurrency is an overall-series surface) and
    ``pool_reference`` mirrors the top-level reference on every bucket — both
    are surfaced so REST and the MCP tool return the identical schema.
    """

    date: str
    key: str | None = None
    max_active: int = 0
    avg_active: float = 0.0
    max_queued: int = 0
    avg_queued: float = 0.0
    pool_reference: int | None = None


class ConcurrencyResponse(BaseModel):
    group_by: str
    date_from: str | None = None
    date_to: str | None = None
    pool_reference: int | None = None
    buckets: list[ConcurrencyBucket]


class GuardrailScorecardScope(BaseModel):
    """Run-level fire counts — how many runs in range carried/triggered guardrails."""

    runs_with_guardrail: int
    runs_with_violations: int
    runs_blocked: int
    first_try_pass_runs: int


class GuardrailFireCounts(BaseModel):
    """Guardrail-level aggregate buckets (summed across the per-run summaries)."""

    bound: int
    evaluated: int
    passed: int
    violated: int
    observed: int
    errored: int
    redacted: int
    skipped: int
    expected_skips: int
    unexpected_skips: int


class GuardrailRates(BaseModel):
    """ADVISORY rates only — the raw detection rate plus the separated
    first-try-pass view. Every metric is labelled advisory and never gates
    autonomy (a raw-pass-rate gate is FAR-218, deferred)."""

    raw_violation_rate: float | None = None
    first_try_pass_rate: float | None = None
    note: str = ""


class GuardrailSelfCorrection(BaseModel):
    """Single-node correction outcomes (FAR-210 T2b trail).

    Reported SEPARATELY from first-try-pass — never merged into a single pass
    rate (Goodhart: retries inflate pass rates; retrying a violation makes a
    naive pass rate look better than the raw detection did).
    """

    corrections_total: int
    converged_clean: int
    escalated_hitl: int
    budget_exhausted: int
    dismissed: int
    in_flight: int
    corrected_pass_rate: float | None = None
    note: str = ""


class GuardrailEvasionBandDrift(BaseModel):
    """Advisory drift signal (canary-band concept, FAR-223 PR C).

    Tracks when ``unexpected_skips`` or the ``errored`` bucket move outside a
    soft band vs the historical baseline. ADVISORY ONLY — never a gate, never
    blocks anything, never changes CI enforcement.
    """

    current_errored_rate: float | None = None
    baseline_errored_rate: float | None = None
    baseline_window_days: int = 0
    unexpected_skips_total: int = 0
    drift_detected: bool = False
    drift_indicator: str = "in_band"
    advisory_only: bool = True
    note: str = ""


class GuardrailScorecardResponse(BaseModel):
    """Advisory guardrail scorecard (FAR-217).

    Read-only, org-scoped, and deliberately advisory: no metric here gates
    autonomy, blocks a run, or changes CI enforcement.
    """

    advisory_only: bool = True
    date_from: str
    date_to: str
    scope: GuardrailScorecardScope
    fire_counts: GuardrailFireCounts
    rates: GuardrailRates
    self_correction: GuardrailSelfCorrection
    evasion_band_drift: GuardrailEvasionBandDrift
    generated_at: str


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


def _analytics_query_filters(
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
) -> AnalyticsParams:
    return _build_params(
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


def _analytics_export_filters(
    dimension: AnalyticsDimension | None = Query(None),
    trigger_type: AnalyticsTriggerType | None = Query(None),
    status: AnalyticsStatus | None = Query(None),
    pipeline_id: list[uuid.UUID] | None = Query(None),
    error_code: str | None = Query(None),
    folder_id: uuid.UUID | None = Query(None),
    date_from: datetime | None = Query(None),
    date_to: datetime | None = Query(None),
    limit: int = Query(_EXPORT_DEFAULT_LIMIT, ge=1, le=_EXPORT_MAX_LIMIT),
) -> AnalyticsParams:
    return _build_params(
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
        detail=MSG_UNEXPECTED_ERROR,
    )


@router.get("/query")
async def analytics_query(
    params: AnalyticsParams = Depends(_analytics_query_filters),
    settings: Settings = Depends(get_settings),
    principal: TenantPrincipal = require_permission(_CODE_ANALYTICS_QUERY),
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


@router.get("/concurrency")
async def analytics_concurrency(
    group_by: AnalyticsGroupBy = Query(AnalyticsGroupBy.DAY),
    auto_granularity: bool = Query(False),
    trigger_type: AnalyticsTriggerType | None = Query(None),
    status: AnalyticsStatus | None = Query(None),
    pipeline_id: list[uuid.UUID] | None = Query(None),
    folder_id: uuid.UUID | None = Query(None),
    date_from: datetime | None = Query(None),
    date_to: datetime | None = Query(None),
    limit: int = Query(1000, ge=1, le=1000),
    settings: Settings = Depends(get_settings),
    principal: TenantPrincipal = require_permission(_CODE_ANALYTICS_QUERY),
    _: object = require_feature("analytics_page"),
) -> ConcurrencyResponse:
    """Slot-utilization series: per-bucket max/avg concurrent active + queued runs.

    Reconstructs "how many runs were running / queued at any instant" from the
    retained fact instants (``[started_at, completed_at)`` overlap — a run
    spanning a bucket boundary counts in both). ``pool_reference`` is the
    binding concurrency cap for the query scope: the org's
    ``run_concurrency_limit``, or the single filtered pipeline's
    ``max_concurrent_runs``. ``dimension``/``error_code`` are not surfaced —
    there is no per-dimension concurrency split.
    """
    org_id = _require_org(principal)
    params = AnalyticsParams(
        group_by=group_by,
        auto_granularity=auto_granularity,
        dimension=None,
        trigger_type=trigger_type,
        status=status,
        pipeline_ids=tuple(pipeline_id or ()),
        error_code=None,
        folder_id=folder_id,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
    )
    try:
        result = await run_concurrency_query(
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
    return ConcurrencyResponse(**result)


@router.get("/guardrails")
async def analytics_guardrails(
    date_from: datetime | None = Query(None),
    date_to: datetime | None = Query(None),
    settings: Settings = Depends(get_settings),
    principal: TenantPrincipal = require_permission(_CODE_ANALYTICS_QUERY),
    _: object = require_feature("analytics_page"),
) -> GuardrailScorecardResponse:
    """Advisory guardrail scorecard (FAR-217) — read-only, never a gate.

    Aggregates the per-run guardrail_summary telemetry (fire counts, raw
    detection rate, first-try-pass), the single-node self-correction trail
    (converged clean / escalated to HITL / budget-exhausted, reported
    SEPARATELY from first-try-pass), and an advisory evasion-band drift signal
    (unexpected skips + errored rate vs baseline). Every metric is advisory:
    nothing here gates autonomy, blocks a run, or changes CI enforcement.
    ``date_from``/``date_to`` accept bare dates or ISO datetimes like the other
    analytics endpoints.
    """
    org_id = _require_org(principal)
    try:
        result = await run_guardrail_scorecard(
            org_id=org_id,
            factory=_analytics_session_factory(settings),
            settings=settings,
            account_id=principal.account_id,
            org_role=principal.org_role,
            date_from=date_from,
            date_to=date_to,
        )
    except Exception as exc:
        if isinstance(exc, HTTPException):
            raise
        raise _map_service_error(exc) from None
    return GuardrailScorecardResponse(**result)


@router.get("/export", response_model=AnalyticsExportResponse)
async def analytics_export(
    format: str = Query("json", pattern="^(json|csv)$"),
    offset: int = Query(0, ge=0),
    params: AnalyticsParams = Depends(_analytics_export_filters),
    settings: Settings = Depends(get_settings),
    principal: TenantPrincipal = require_permission(_CODE_ANALYTICS_QUERY),
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
    try:
        result = await export_facts(
            org_id=org_id,
            params=params,
            factory=_analytics_session_factory(settings),
            settings=settings,
            account_id=principal.account_id,
            org_role=principal.org_role,
            offset=offset,
            limit=params.limit,
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
