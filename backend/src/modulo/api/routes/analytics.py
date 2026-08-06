"""GET /api/v1/analytics/query — typed-params analytics over run_daily_facts (ADR 020).

The backend is the SOLE bucketing authority: day/ISO-week bucketing and
zero-fill happen here (``bucket_rows``), never on the client. Tenant isolation
relies on the EXPLICIT ``organisation_id = :org`` predicate injected by the
SQL builder (modulo_app is BYPASSRLS on Postgres and the ORM tenant filter is
NOT registered there) — RLS via ``set_rls_org`` is defense-in-depth, not the
control. Every request sets a bounded ``statement_timeout`` so a runaway date
range degrades to a clean 503 instead of hogging a pooled connection.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import UTC, date, datetime, timedelta
from datetime import time as dt_time

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette import status as http_status

from modulo.api.dependencies import get_or_create_engine, require_feature, require_permission
from modulo.auth.jwt import TenantPrincipal
from modulo.core.analytics.builder import (
    AnalyticsDimension,
    AnalyticsGroupBy,
    AnalyticsQuery,
    AnalyticsStatus,
    AnalyticsTriggerType,
    bucket_rows,
    build_facts_query,
    resolve_group_by,
)
from modulo.db.rls import set_rls_org, set_rls_user_context
from modulo.settings import Settings, get_settings

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/analytics", tags=["analytics"])

# Default statement timeout for analytics queries (ms) — settings-driven via
# ``analytics_query_statement_timeout_ms`` when configured.
_DEFAULT_STATEMENT_TIMEOUT_MS = 5000

# Per-org app-level limiter (simple in-memory): 60 requests/minute. Best-effort
# and bounded: idle orgs are pruned and the number of tracked orgs is capped, so
# the dict cannot grow without limit across many orgs. It remains process-local
# and is therefore ineffective across multiple worker processes — a shared
# limiter (e.g. Redis) is the production-grade replacement for this fallback.
_RATE_LIMIT_WINDOW_SECONDS = 60
_RATE_LIMIT_MAX_PER_ORG = 60
_RATE_LIMIT_MAX_ORGS = 1000
_rate_hits: dict[str, list[float]] = {}


class AnalyticsBucket(BaseModel):
    date: str
    key: str | None = None
    count: int = 0
    total_cost_usd: float | None = None
    total_tokens: int | None = None
    avg_duration_ms: float | None = None
    success_rate: float | None = None


class AnalyticsResponse(BaseModel):
    group_by: str
    dimension: str | None = None
    date_from: str | None = None
    date_to: str | None = None
    buckets: list[AnalyticsBucket]


def _rate_limited(org_id: str) -> bool:
    now = time.monotonic()
    _prune_rate_hits(now)
    hits = _rate_hits.setdefault(org_id, [])
    hits[:] = [t for t in hits if now - t < _RATE_LIMIT_WINDOW_SECONDS]
    if len(hits) >= _RATE_LIMIT_MAX_PER_ORG:
        return True
    hits.append(now)
    return False


def _prune_rate_hits(now: float) -> None:
    """Drop idle orgs and cap the number of tracked orgs (best-effort bound).

    An org with no request inside the rate window is forgotten entirely; if that
    is still not enough, the least-recently-active orgs are evicted until the
    cap is met.
    """
    cutoff = now - _RATE_LIMIT_WINDOW_SECONDS
    for oid in [oid for oid, hits in _rate_hits.items() if not hits or hits[-1] <= cutoff]:
        del _rate_hits[oid]
    while len(_rate_hits) > _RATE_LIMIT_MAX_ORGS:
        oldest = min(_rate_hits, key=lambda oid: _rate_hits[oid][-1] if _rate_hits[oid] else 0.0)
        del _rate_hits[oldest]


def _is_query_canceled(exc: DBAPIError) -> bool:
    """Detect a Postgres statement-timeout cancellation (SQLSTATE 57014).

    The asyncpg dialect wraps the driver error (``AsyncAdapt_asyncpg_dbapi.Error``),
    so the type-name check must unwrap ``orig``/``__cause__`` and fall back to
    the standard ``query_canceled`` SQLSTATE.
    """
    names = {"QueryCanceledError", "QueryCanceled"}
    orig = exc.orig
    if orig is not None and type(orig).__name__ in names:
        return True
    if orig is not None:
        for candidate in (getattr(orig, "orig", None), getattr(orig, "__cause__", None)):
            if candidate is not None and type(candidate).__name__ in names:
                return True
        if getattr(orig, "sqlstate", None) == "57014":
            return True
    return False


def _analytics_session_factory(settings: Settings) -> async_sessionmaker[AsyncSession]:
    """Dedicated sessionmaker over the EXISTING shared engine (autobegin=False)."""
    return async_sessionmaker(get_or_create_engine(settings), expire_on_commit=False, autobegin=False)


@router.get("/query", response_model=AnalyticsResponse)
async def analytics_query(
    group_by: AnalyticsGroupBy = Query(AnalyticsGroupBy.DAY),
    auto_granularity: bool = Query(False),
    dimension: AnalyticsDimension | None = Query(None),
    trigger_type: AnalyticsTriggerType | None = Query(None),
    status: AnalyticsStatus | None = Query(None),
    pipeline_id: uuid.UUID | None = Query(None),
    folder_id: uuid.UUID | None = Query(None),
    date_from: datetime | None = Query(None),
    date_to: datetime | None = Query(None),
    limit: int = Query(1000, ge=1, le=1000),
    settings: Settings = Depends(get_settings),
    principal: TenantPrincipal = require_permission("analytics.query"),
    _: object = require_feature("analytics_page"),
) -> AnalyticsResponse:
    """Bucketed run-facts series over the requested range, grouped hour/day/ISO-week.

    ``date_from``/``date_to`` accept bare dates ("2026-08-06", parsed as midnight
    UTC) or ISO datetimes ("2026-08-06T14:00:00Z"). ``auto_granularity=true``
    overrides ``group_by`` from the effective range span (hour ≤3d, day ≤90d,
    week otherwise).
    """
    org_id = principal.organisation_id
    if org_id is None:
        raise HTTPException(
            status_code=http_status.HTTP_403_FORBIDDEN,
            detail="Analytics requires an organisation context",
        )
    if _rate_limited(str(org_id)):
        raise HTTPException(status_code=http_status.HTTP_429_TOO_MANY_REQUESTS, detail="Rate limit exceeded")

    today = datetime.now(UTC).date()
    effective_to = date_to or today
    effective_from = date_from or (effective_to - timedelta(days=364))
    if effective_from > effective_to:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="date_from must be <= date_to",
        )
    if (effective_to - effective_from).days > 365:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="date range must be 365 days or less",
        )

    # Normalise to aware UTC datetimes: bare dates expand to 00:00 / 23:59:59 so
    # hourly bucketing covers the whole day.
    def _utc_dt(value: date | datetime, *, end_of_day: bool) -> datetime:
        if isinstance(value, datetime):
            return value.replace(tzinfo=UTC) if value.tzinfo is None else value
        if end_of_day:
            return datetime.combine(value, dt_time(23, 59, 59), tzinfo=UTC)
        return datetime.combine(value, dt_time.min, tzinfo=UTC)

    effective_from = _utc_dt(effective_from, end_of_day=False)
    effective_to = _utc_dt(effective_to, end_of_day=True)

    effective_group_by = resolve_group_by(group_by, effective_from, effective_to) if auto_granularity else group_by

    query = AnalyticsQuery(
        org_id=org_id,
        group_by=effective_group_by,
        dimension=dimension,
        trigger_type=trigger_type,
        status=status,
        pipeline_id=pipeline_id,
        folder_id=folder_id,
        date_from=effective_from,
        date_to=effective_to,
        limit=limit,
    )
    stmt, params = build_facts_query(query)

    factory = _analytics_session_factory(settings)
    async with factory() as session:
        try:
            async with session.begin():
                await set_rls_org(session, org_id)
                await set_rls_user_context(session, principal.account_id, principal.org_role)
                dialect = (await session.connection()).dialect.name
                if dialect == "postgresql":
                    timeout_ms = getattr(
                        settings, "analytics_query_statement_timeout_ms", _DEFAULT_STATEMENT_TIMEOUT_MS
                    )
                    await session.execute(text("SELECT set_config('timezone', 'UTC', true)"))
                    await session.execute(
                        text("SELECT set_config('statement_timeout', :ms, true)"),
                        {"ms": str(int(timeout_ms))},
                    )
                result = await session.execute(stmt, params)
                rows = result.all()
        except asyncio.CancelledError:
            raise
        except ProgrammingError:
            # ProgrammingError must be caught BEFORE DBAPIError: it is a
            # DatabaseError subclass, so the broader branch would swallow it and
            # return 503. A missing table/column means migrations haven't run —
            # a 501 is the actionable signal.
            _log.exception("analytics.query.programming_error", extra={"org_id": str(org_id)})
            raise HTTPException(
                status_code=http_status.HTTP_501_NOT_IMPLEMENTED,
                detail="Feature is not available. Run database migrations to enable it.",
            ) from None
        except DBAPIError as exc:
            if _is_query_canceled(exc):
                _log.warning(
                    "analytics.query.timeout",
                    extra={"org_id": str(org_id), "date_from": str(effective_from), "date_to": str(effective_to)},
                )
                raise HTTPException(
                    status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="query exceeded timeout — reduce the date range",
                ) from None
            _log.exception("analytics.query.db_error", extra={"org_id": str(org_id)})
            raise HTTPException(
                status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Database temporarily unavailable.",
            ) from None
        except SQLAlchemyError:
            _log.exception("analytics.query.db_error", extra={"org_id": str(org_id)})
            raise HTTPException(
                status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Database temporarily unavailable.",
            ) from None
        except Exception:
            _log.exception("analytics.query.unexpected_error", extra={"org_id": str(org_id)})
            raise HTTPException(
                status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="An unexpected error occurred.",
            ) from None

    buckets = bucket_rows(
        list(rows),
        group_by=effective_group_by,
        dimension=dimension,
        date_from=effective_from,
        date_to=effective_to,
        limit=limit,
    )
    return AnalyticsResponse(
        group_by=effective_group_by.value,
        dimension=dimension.value if dimension is not None else None,
        date_from=effective_from.isoformat(),
        date_to=effective_to.isoformat(),
        buckets=[AnalyticsBucket(**b) for b in buckets],
    )
