"""Shared analytics query execution service (ADR 020, FAR-102).

The REST route (``modulo.api.routes.analytics``) and the ``query_analytics``
MCP tool both funnel through this service so the org predicate, rate limit,
statement timeout, validation, bucketing, and error semantics stay identical
across surfaces.

Isolation invariant (CRITICAL — carried over from the route): ``modulo_app``
is NOBYPASSRLS (tenant isolation relies on RLS policies) and the ORM tenant
filter is NOT registered on Postgres — the explicit ``organisation_id = :org``
predicate injected by the builder is the PRIMARY isolation control (RLS also
enforces org scoping). ``set_rls_org`` remains defense-in-depth. The ``factory``
(sessionmaker over the shared engine) is supplied by
the caller so this core module never imports ``modulo.api`` (import-linter).

The service never raises FastAPI exceptions — it raises the typed
``AnalyticsError`` subclasses below, which each caller maps to its surface
(HTTP status codes / MCP error dicts).
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from modulo.core.analytics.builder import (
    CONCURRENCY_MAX_RAW_ROWS,
    HOUR_GROUPBY_MAX_RANGE_DAYS,
    AnalyticsDimension,
    AnalyticsGroupBy,
    AnalyticsQuery,
    AnalyticsStatus,
    AnalyticsTriggerType,
    bucket_concurrency_rows,
    bucket_rows,
    build_concurrency_query,
    build_error_code_condition,
    build_facts_query,
    hour_groupby_span_exceeds,
    resolve_group_by,
    to_utc_aware,
)
from modulo.db.crud.run import get_org_run_concurrency_limit
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.run import TERMINAL_STATUSES
from modulo.db.models.run_daily_facts import RunDailyFact
from modulo.db.rls import set_rls_org, set_rls_user_context
from modulo.settings import Settings

_log = logging.getLogger(__name__)

__all__ = [
    "EXPORT_COLUMN_NAMES",
    "AnalyticsDatabaseError",
    "AnalyticsError",
    "AnalyticsMigrationRequiredError",
    "AnalyticsParams",
    "AnalyticsQueryTimeoutError",
    "AnalyticsRateLimitedError",
    "AnalyticsValidationError",
    "export_facts",
    "run_analytics_query",
    "run_concurrency_query",
]

# Default statement timeout for analytics queries (ms) — settings-driven via
# ``analytics_query_statement_timeout_ms`` when configured.
_DEFAULT_STATEMENT_TIMEOUT_MS = 5000

# Repeated SQL fragments and typed error messages (S1192). Constants are pure
# aliases — the SQL text and error strings are part of the surface contract.
_SQL_SET_STATEMENT_TIMEOUT = "SELECT set_config('statement_timeout', :ms, true)"
_SQL_SET_TIMEZONE_UTC = "SELECT set_config('timezone', 'UTC', true)"
_ERR_DATABASE_UNAVAILABLE = "Database temporarily unavailable."
_ERR_RATE_LIMIT_EXCEEDED = "Rate limit exceeded"

# Max date range accepted by the bucketed query (matches the old route guard).
_MAX_QUERY_RANGE_DAYS = 365

# Facts-freshness stale threshold (hours): the analytics response is flagged
# ``facts_stale`` when the org's newest day with a TERMINAL-status fact row is
# older than this (~36h, matching the ticket's 24-36h window). Facts are
# materialized asynchronously (live writer at run completion + a daily 01:00
# UTC backfill cron), so a stale flag is a "the numbers for recent days may be
# incomplete" signal — the frontend surfaces it as a notice, never a hard block.
_FRESHNESS_STALE_HOURS = 36

# Export pagination bounds (FAR-102, Part D).
_EXPORT_DEFAULT_LIMIT = 500
_EXPORT_MAX_LIMIT = 5000

# Per-org app-level limiter (simple in-memory): 60 requests/minute. Best-effort
# and bounded: idle orgs are pruned and the number of tracked orgs is capped, so
# the dict cannot grow without limit across many orgs. It remains process-local
# and is therefore ineffective across multiple worker processes — a shared
# limiter (e.g. Redis) is the production-grade replacement for this fallback.
_RATE_LIMIT_WINDOW_SECONDS = 60
_RATE_LIMIT_MAX_PER_ORG = 60
_RATE_LIMIT_MAX_ORGS = 1000
_rate_hits: dict[str, list[float]] = {}


class AnalyticsError(Exception):
    """Base class for typed analytics service errors."""


class AnalyticsRateLimitedError(AnalyticsError):
    """The org exceeded the per-window request budget."""


class AnalyticsValidationError(AnalyticsError):
    """A typed parameter was invalid (bad range, bad granularity, ...)."""

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(detail)


class AnalyticsQueryTimeoutError(AnalyticsError):
    """The statement exceeded the bounded ``statement_timeout``."""


class AnalyticsMigrationRequiredError(AnalyticsError):
    """A required table/column is missing — migrations have not run."""


class AnalyticsDatabaseError(AnalyticsError):
    """An unexpected database failure."""


@dataclass(frozen=True)
class AnalyticsParams:
    """Typed parameters mirroring the REST / MCP surface (FAR-102, Part C)."""

    group_by: AnalyticsGroupBy = AnalyticsGroupBy.DAY
    auto_granularity: bool = False
    dimension: AnalyticsDimension | None = None
    trigger_type: AnalyticsTriggerType | None = None
    status: AnalyticsStatus | None = None
    pipeline_ids: tuple[uuid.UUID, ...] = ()
    team_id: uuid.UUID | None = None
    error_code: str | None = None
    folder_id: uuid.UUID | None = None
    date_from: datetime | None = None
    date_to: datetime | None = None
    limit: int = 1000


# ---------------------------------------------------------------------------
# Rate limiter (moved verbatim from the old route so both surfaces share it)
# ---------------------------------------------------------------------------


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
    """Drop idle orgs and cap the number of tracked orgs (best-effort bound)."""
    cutoff = now - _RATE_LIMIT_WINDOW_SECONDS
    for oid in [oid for oid, hits in _rate_hits.items() if not hits or hits[-1] <= cutoff]:
        del _rate_hits[oid]
    while len(_rate_hits) > _RATE_LIMIT_MAX_ORGS:
        oldest = min(_rate_hits, key=lambda oid: _rate_hits[oid][-1] if _rate_hits[oid] else 0.0)
        del _rate_hits[oldest]


def _is_query_canceled(exc: DBAPIError) -> bool:
    """Detect a Postgres statement-timeout cancellation (SQLSTATE 57014)."""
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


# ---------------------------------------------------------------------------
# Bounds normalisation + validation (moved verbatim from the old route)
# ---------------------------------------------------------------------------


def _normalise_bounds(
    date_from: datetime | None,
    date_to: datetime | None,
) -> tuple[datetime, datetime]:
    """Normalise both bounds to aware UTC, expanding bare dates to the whole day.

    Raises ``AnalyticsValidationError`` for inverted or over-wide ranges and for
    an explicit hour granularity over a >14-day range (grid amplification).
    """
    today = datetime.now(UTC).date()
    effective_to = date_to or today
    effective_from = date_from or (effective_to - timedelta(days=364))

    effective_from = to_utc_aware(effective_from)
    effective_to = to_utc_aware(effective_to, end_of_day=True)

    if effective_from > effective_to:
        raise AnalyticsValidationError("date_from must be <= date_to")
    if (effective_to - effective_from).days > _MAX_QUERY_RANGE_DAYS:
        raise AnalyticsValidationError("date range must be 365 days or less")
    return effective_from, effective_to


def _check_hour_cap(
    group_by: AnalyticsGroupBy,
    effective_from: datetime,
    effective_to: datetime,
) -> None:
    if group_by == AnalyticsGroupBy.HOUR and hour_groupby_span_exceeds(effective_from, effective_to):
        raise AnalyticsValidationError(
            f"hour granularity supports ranges of {HOUR_GROUPBY_MAX_RANGE_DAYS} days or less"
        )


async def _facts_freshness(
    factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    *,
    org_id: uuid.UUID,
    account_id: uuid.UUID | None,
    org_role: str | None,
) -> tuple[float | None, bool]:
    """How long since the org's newest RELIABLE fact day, and whether it is stale.

    ``run_daily_facts`` is materialized asynchronously (the live writer at run
    completion plus a daily 01:00 UTC backfill cron), so a query for the most
    recent window can silently return zeros/missing rows while the facts for
    those days are still being written — or, worse, the writer is broken
    (FAR-200: facts stamped with the pre-write ``'running'`` status and NULL
    cost). The freshness signal is the newest ``run_date`` that has AT LEAST ONE
    TERMINAL-status fact row: a writer that stops producing terminal facts makes
    this lag even when raw (non-terminal) rows for today exist.

    Returns ``(freshness_hours, stale)``:

    - ``freshness_hours`` — hours since that day's UTC boundary (``0.0`` when
      the newest reliable day is today; ``None`` when the org has no terminal
      fact at all — a brand-new/empty org reads as "no data yet", not "stale").
    - ``stale`` — ``True`` when ``freshness_hours`` exceeds
      ``_FRESHNESS_STALE_HOURS``.

    Fail-open: any error degrades to ``(None, False)`` with a log, so the
    staleness indicator can never break the analytics query itself.
    """
    try:
        async with factory() as session:
            try:
                async with session.begin():
                    await set_rls_org(session, org_id)
                    if account_id is not None:
                        await set_rls_user_context(session, account_id, org_role or "")
                    dialect = (await session.connection()).dialect.name
                    if dialect == "postgresql":
                        timeout_ms = getattr(
                            settings, "analytics_query_statement_timeout_ms", _DEFAULT_STATEMENT_TIMEOUT_MS
                        )
                        await session.execute(
                            text(_SQL_SET_STATEMENT_TIMEOUT),
                            {"ms": str(int(timeout_ms))},
                        )
                    newest_terminal_day = (
                        await session.execute(
                            sa.select(sa.func.max(RunDailyFact.run_date)).where(
                                RunDailyFact.organisation_id == org_id,
                                RunDailyFact.status.in_(TERMINAL_STATUSES),
                            )
                        )
                    ).scalar_one_or_none()
            except asyncio.CancelledError:
                raise
            except (ProgrammingError, SQLAlchemyError):
                _log.exception("analytics.freshness.db_error", extra={"org_id": str(org_id)})
                return None, False
    except Exception:
        _log.exception("analytics.freshness.unexpected_error", extra={"org_id": str(org_id)})
        return None, False

    if newest_terminal_day is None:
        return None, False
    newest_instant = datetime.combine(newest_terminal_day, datetime.min.time(), tzinfo=UTC)
    freshness_hours = (datetime.now(UTC) - newest_instant).total_seconds() / 3600.0
    return round(freshness_hours, 1), freshness_hours > _FRESHNESS_STALE_HOURS


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


async def _execute_with_guards(
    factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    *,
    org_id: uuid.UUID,
    account_id: uuid.UUID | None,
    org_role: str | None,
    stmt: Any,
    params: dict[str, Any],
) -> list[Any]:
    """Run *stmt* inside a transaction with RLS context + bounded statement timeout.

    Maps database failures to the typed ``AnalyticsError`` subclasses so both
    the REST route and the MCP tool surface identical semantics.
    """
    try:
        async with factory() as session:
            try:
                async with session.begin():
                    await set_rls_org(session, org_id)
                    if account_id is not None:
                        await set_rls_user_context(session, account_id, org_role or "")
                    dialect = (await session.connection()).dialect.name
                    if dialect == "postgresql":
                        timeout_ms = getattr(
                            settings, "analytics_query_statement_timeout_ms", _DEFAULT_STATEMENT_TIMEOUT_MS
                        )
                        await session.execute(text(_SQL_SET_TIMEZONE_UTC))
                        await session.execute(
                            text(_SQL_SET_STATEMENT_TIMEOUT),
                            {"ms": str(int(timeout_ms))},
                        )
                    result = await session.execute(stmt, params)
                    return list(result.all())
            except asyncio.CancelledError:
                raise
            except ProgrammingError:
                _log.exception("analytics.query.programming_error", extra={"org_id": str(org_id)})
                raise AnalyticsMigrationRequiredError(
                    "Feature is not available. Run database migrations to enable it."
                ) from None
            except DBAPIError as exc:
                if _is_query_canceled(exc):
                    _log.warning("analytics.query.timeout", extra={"org_id": str(org_id)})
                    raise AnalyticsQueryTimeoutError("query exceeded timeout — reduce the date range") from None
                _log.exception("analytics.query.db_error", extra={"org_id": str(org_id)})
                raise AnalyticsDatabaseError(_ERR_DATABASE_UNAVAILABLE) from None
            except SQLAlchemyError:
                _log.exception("analytics.query.db_error", extra={"org_id": str(org_id)})
                raise AnalyticsDatabaseError(_ERR_DATABASE_UNAVAILABLE) from None
    except (AnalyticsError, asyncio.CancelledError):
        raise
    except Exception:
        _log.exception("analytics.query.unexpected_error", extra={"org_id": str(org_id)})
        raise AnalyticsDatabaseError(_ERR_DATABASE_UNAVAILABLE) from None


async def run_analytics_query(
    *,
    org_id: uuid.UUID,
    params: AnalyticsParams,
    factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    account_id: uuid.UUID | None = None,
    org_role: str | None = None,
) -> dict[str, Any]:
    """Execute the bucketed analytics query and return the response shape.

    Returns a dict in the ``AnalyticsResponse`` shape (``group_by``,
    ``dimension``, ``date_from``, ``date_to``, ``buckets``). Raises the typed
    ``AnalyticsError`` subclasses for rate-limit / validation / DB failures.
    """
    if _rate_limited(str(org_id)):
        raise AnalyticsRateLimitedError(_ERR_RATE_LIMIT_EXCEEDED)

    effective_from, effective_to = _normalise_bounds(params.date_from, params.date_to)
    effective_group_by = (
        resolve_group_by(params.group_by, effective_from, effective_to) if params.auto_granularity else params.group_by
    )
    _check_hour_cap(effective_group_by, effective_from, effective_to)

    query = AnalyticsQuery(
        org_id=org_id,
        group_by=effective_group_by,
        dimension=params.dimension,
        trigger_type=params.trigger_type,
        status=params.status,
        pipeline_ids=params.pipeline_ids,
        team_id=params.team_id,
        error_code=params.error_code,
        folder_id=params.folder_id,
        date_from=effective_from,
        date_to=effective_to,
        limit=params.limit,
    )
    stmt, bind = build_facts_query(query)
    rows = await _execute_with_guards(
        factory,
        settings,
        org_id=org_id,
        account_id=account_id,
        org_role=org_role,
        stmt=stmt,
        params=bind,
    )
    buckets = bucket_rows(
        rows,
        group_by=effective_group_by,
        dimension=params.dimension,
        date_from=effective_from,
        date_to=effective_to,
        limit=params.limit,
    )
    freshness_hours, stale = await _facts_freshness(
        factory,
        settings,
        org_id=org_id,
        account_id=account_id,
        org_role=org_role,
    )
    return {
        "group_by": effective_group_by.value,
        "dimension": params.dimension.value if params.dimension is not None else None,
        "date_from": effective_from.isoformat(),
        "date_to": effective_to.isoformat(),
        "facts_freshness_hours": freshness_hours,
        "facts_stale": stale,
        "buckets": buckets,
    }


async def _resolve_pool_reference(
    factory: async_sessionmaker[AsyncSession],
    _settings: Settings,
    *,
    org_id: uuid.UUID,
    account_id: uuid.UUID | None,
    org_role: str | None,
    pipeline_ids: tuple[uuid.UUID, ...],
) -> int | None:
    """Best-effort concurrency-cap reference for the response (FAR-134).

    Never raises — a failed reference degrades to ``None`` with a log, never a
    failed query. With a single ``pipeline_id`` filter the pool reference is
    that pipeline's ``max_concurrent_runs`` (the binding cap for a one-pipeline
    query); otherwise it is the org's ``run_concurrency_limit``. Reads use the
    explicit org predicate because ``modulo_app`` is NOBYPASSRLS (tenant
    isolation relies on RLS policies) — the predicate is the PRIMARY isolation
    control (RLS also enforces org scoping), and ``session.get`` alone would not
    scope it.
    """
    try:
        async with factory() as session:
            try:
                async with session.begin():
                    await set_rls_org(session, org_id)
                    if account_id is not None:
                        await set_rls_user_context(session, account_id, org_role or "")
                    if len(pipeline_ids) == 1:
                        value = (
                            await session.execute(
                                sa.select(Pipeline.max_concurrent_runs).where(
                                    Pipeline.id == pipeline_ids[0],
                                    Pipeline.organisation_id == org_id,
                                )
                            )
                        ).scalar_one_or_none()
                        return int(value) if value is not None else None
                    return await get_org_run_concurrency_limit(session, org_id)
            except asyncio.CancelledError:
                raise
            except (ProgrammingError, SQLAlchemyError):
                _log.exception("analytics.pool_reference.db_error", extra={"org_id": str(org_id)})
                return None
    except Exception:
        _log.exception("analytics.pool_reference.unexpected_error", extra={"org_id": str(org_id)})
        return None


async def run_concurrency_query(
    *,
    org_id: uuid.UUID,
    params: AnalyticsParams,
    factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    account_id: uuid.UUID | None = None,
    org_role: str | None = None,
) -> dict[str, Any]:
    """Slot-utilization series: per-bucket max/avg active + queued runs.

    Mirrors ``run_analytics_query`` (rate limit, bounds normalisation,
    auto-granularity, hour cap, statement timeout) but buckets the overlap of
    the runs' ``[started_at, completed_at)`` intervals in Python instead of a
    GROUP BY. ``dimension`` is accepted for surface parity and ignored — there
    is no per-dimension concurrency split. The raw scan is bounded by
    ``CONCURRENCY_MAX_RAW_ROWS``: when the filtered scan exceeds the cap the
    query raises ``AnalyticsValidationError`` telling the caller to narrow the
    range — it never silently truncates (a partial scan would yield wrong
    max/avg counts). Returns ``{group_by, date_from, date_to, pool_reference,
    buckets}`` where each bucket carries ``{date, key: None, max_active,
    avg_active, max_queued, avg_queued, pool_reference}``.
    """
    if _rate_limited(str(org_id)):
        raise AnalyticsRateLimitedError(_ERR_RATE_LIMIT_EXCEEDED)

    effective_from, effective_to = _normalise_bounds(params.date_from, params.date_to)
    effective_group_by = (
        resolve_group_by(params.group_by, effective_from, effective_to) if params.auto_granularity else params.group_by
    )
    _check_hour_cap(effective_group_by, effective_from, effective_to)

    query = AnalyticsQuery(
        org_id=org_id,
        group_by=effective_group_by,
        dimension=None,
        trigger_type=params.trigger_type,
        status=params.status,
        pipeline_ids=params.pipeline_ids,
        team_id=params.team_id,
        error_code=params.error_code,
        folder_id=params.folder_id,
        date_from=effective_from,
        date_to=effective_to,
        limit=params.limit,
    )
    stmt, bind = build_concurrency_query(query)
    rows = await _execute_with_guards(
        factory,
        settings,
        org_id=org_id,
        account_id=account_id,
        org_role=org_role,
        stmt=stmt,
        params=bind,
    )
    if len(rows) > CONCURRENCY_MAX_RAW_ROWS:
        raise AnalyticsValidationError(
            f"concurrency query exceeded the {CONCURRENCY_MAX_RAW_ROWS}-row raw cap — "
            "reduce the date range or add pipeline/status filters"
        )
    buckets = bucket_concurrency_rows(
        rows,
        group_by=effective_group_by,
        date_from=effective_from,
        date_to=effective_to,
        limit=params.limit,
    )
    pool_reference = await _resolve_pool_reference(
        factory,
        settings,
        org_id=org_id,
        account_id=account_id,
        org_role=org_role,
        pipeline_ids=params.pipeline_ids,
    )
    return {
        "group_by": effective_group_by.value,
        "date_from": effective_from.isoformat(),
        "date_to": effective_to.isoformat(),
        "pool_reference": pool_reference,
        "buckets": [{**bucket, "key": None, "pool_reference": pool_reference} for bucket in buckets],
    }


# ---------------------------------------------------------------------------
# Export (FAR-102, Part D)
# ---------------------------------------------------------------------------

# Raw fact rows are exported column-for-column (all fact columns, including the
# FAR-102 enrichment). ``id``/``organisation_id``/``updated_at`` are omitted —
# internal plumbing, not analytics surface.
_EXPORT_COLUMNS: tuple[Any, ...] = (
    RunDailyFact.run_id,
    RunDailyFact.run_date,
    RunDailyFact.team_id,
    RunDailyFact.team_name,
    RunDailyFact.pipeline_id,
    RunDailyFact.pipeline_name,
    RunDailyFact.folder_id,
    RunDailyFact.trigger_type,
    RunDailyFact.status,
    RunDailyFact.total_cost_usd,
    RunDailyFact.total_tokens,
    RunDailyFact.duration_ms,
    RunDailyFact.error_code,
    RunDailyFact.claim_count,
    RunDailyFact.queue_wait_ms,
    RunDailyFact.final_idle_ms,
    RunDailyFact.cancellation_requested,
    RunDailyFact.dispatcher,
    RunDailyFact.node_count,
    RunDailyFact.sandbox_agent_node_count,
    RunDailyFact.max_node_timeout_seconds,
    RunDailyFact.parent_run_id,
    RunDailyFact.snapshot_id,
    RunDailyFact.run_number,
    RunDailyFact.output_bytes,
    RunDailyFact.rate_limited,
    RunDailyFact.created_at,
)

_EXPORT_COLUMN_NAMES: tuple[str, ...] = tuple(c.name for c in _EXPORT_COLUMNS)
# Public alias for callers that build CSV/text surfaces from the column list.
EXPORT_COLUMN_NAMES: tuple[str, ...] = _EXPORT_COLUMN_NAMES


def _export_filters(
    *,
    org_id: uuid.UUID,
    params: AnalyticsParams,
    effective_from: datetime,
    effective_to: datetime,
) -> tuple[list[Any], dict[str, Any]]:
    """Parameterised WHERE clauses + bound params shared by count and page queries."""
    conditions: list[Any] = [RunDailyFact.organisation_id == sa.bindparam("org_id", type_=sa.Uuid)]
    bind: dict[str, Any] = {"org_id": org_id}
    conditions.append(RunDailyFact.run_date >= sa.bindparam("date_from", type_=sa.Date))
    conditions.append(RunDailyFact.run_date <= sa.bindparam("date_to", type_=sa.Date))
    bind["date_from"] = effective_from.date()
    bind["date_to"] = effective_to.date()
    if params.trigger_type is not None:
        conditions.append(RunDailyFact.trigger_type == sa.bindparam("trigger_type", type_=sa.String))
        bind["trigger_type"] = params.trigger_type.value
    if params.status is not None:
        conditions.append(RunDailyFact.status == sa.bindparam("status", type_=sa.String))
        bind["status"] = params.status.value
    if params.pipeline_ids:
        conditions.append(RunDailyFact.pipeline_id.in_(sa.bindparam("pipeline_ids", type_=sa.Uuid, expanding=True)))
        bind["pipeline_ids"] = list(params.pipeline_ids)
    if params.error_code is not None:
        # The facts table stores the RAW DB code while the runs API emits
        # dotted codes — a filter must match every spelling of the same code.
        # The aggregate "Unknown error" slice (harness.unknown) is special:
        # build_error_code_condition turns it into a NOT IN over the complement
        # of known_error_codes() so it matches the same raw rows the chart
        # slice shows (see the helper docstring).
        conditions.append(build_error_code_condition(bind, params.error_code))
    if params.folder_id is not None:
        conditions.append(RunDailyFact.folder_id == sa.bindparam("folder_id", type_=sa.Uuid))
        bind["folder_id"] = params.folder_id
    return conditions, bind


def _serialize_fact_row(row: Any) -> dict[str, Any]:
    """Serialise one raw fact row to JSON-safe values keyed by column name."""
    out: dict[str, Any] = {}
    for name in _EXPORT_COLUMN_NAMES:
        value = getattr(row, name)
        if isinstance(value, uuid.UUID):
            out[name] = str(value)
        elif isinstance(value, datetime):
            out[name] = value.isoformat() if value.tzinfo is not None else value.replace(tzinfo=UTC).isoformat()
        elif isinstance(value, Decimal):
            out[name] = float(value)
        elif isinstance(value, date):
            out[name] = value.isoformat()
        else:
            out[name] = value
    return out


async def export_facts(
    *,
    org_id: uuid.UUID,
    params: AnalyticsParams,
    factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    account_id: uuid.UUID | None = None,
    org_role: str | None = None,
    offset: int = 0,
    limit: int = _EXPORT_DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Return raw fact rows (no bucketing) filtered by the same typed params.

    Paginated by ``offset``/``limit`` (default 500, max 5000), ordered by
    ``run_date``, ``created_at`` then ``run_id`` for a stable cursor. ``dimension``
    is accepted for surface parity but ignored — export has no bucketing.
    """
    if _rate_limited(str(org_id)):
        raise AnalyticsRateLimitedError(_ERR_RATE_LIMIT_EXCEEDED)

    effective_from, effective_to = _normalise_bounds(params.date_from, params.date_to)
    conditions, bind = _export_filters(
        org_id=org_id,
        params=params,
        effective_from=effective_from,
        effective_to=effective_to,
    )

    count_stmt = sa.select(sa.func.count(RunDailyFact.id)).where(*conditions)
    rows_stmt = (
        sa.select(*_EXPORT_COLUMNS)
        .where(*conditions)
        .order_by(RunDailyFact.run_date, RunDailyFact.created_at, RunDailyFact.run_id)
        .offset(offset)
        .limit(limit)
    )

    total = 0
    rows: list[Any] = []
    try:
        async with factory() as session:
            try:
                async with session.begin():
                    await set_rls_org(session, org_id)
                    if account_id is not None:
                        await set_rls_user_context(session, account_id, org_role or "")
                    dialect = (await session.connection()).dialect.name
                    if dialect == "postgresql":
                        timeout_ms = getattr(
                            settings, "analytics_query_statement_timeout_ms", _DEFAULT_STATEMENT_TIMEOUT_MS
                        )
                        await session.execute(text(_SQL_SET_TIMEZONE_UTC))
                        await session.execute(
                            text(_SQL_SET_STATEMENT_TIMEOUT),
                            {"ms": str(int(timeout_ms))},
                        )
                    total = int((await session.execute(count_stmt, bind)).scalar_one())
                    result = await session.execute(rows_stmt, bind)
                    rows = list(result.all())
            except asyncio.CancelledError:
                raise
            except ProgrammingError:
                _log.exception("analytics.export.programming_error", extra={"org_id": str(org_id)})
                raise AnalyticsMigrationRequiredError(
                    "Feature is not available. Run database migrations to enable it."
                ) from None
            except DBAPIError as exc:
                if _is_query_canceled(exc):
                    _log.warning("analytics.export.timeout", extra={"org_id": str(org_id)})
                    raise AnalyticsQueryTimeoutError("query exceeded timeout — reduce the date range") from None
                _log.exception("analytics.export.db_error", extra={"org_id": str(org_id)})
                raise AnalyticsDatabaseError(_ERR_DATABASE_UNAVAILABLE) from None
            except SQLAlchemyError:
                _log.exception("analytics.export.db_error", extra={"org_id": str(org_id)})
                raise AnalyticsDatabaseError(_ERR_DATABASE_UNAVAILABLE) from None
    except (AnalyticsError, asyncio.CancelledError):
        raise
    except Exception:
        _log.exception("analytics.export.unexpected_error", extra={"org_id": str(org_id)})
        raise AnalyticsDatabaseError(_ERR_DATABASE_UNAVAILABLE) from None

    return {
        "items": [_serialize_fact_row(r) for r in rows],
        "total": total,
        "offset": offset,
        "limit": limit,
    }
