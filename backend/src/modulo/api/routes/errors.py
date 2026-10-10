"""Error tracking API — session-key generation, event ingestion, and dashboard."""

from __future__ import annotations

import json
import logging
import time as _time
import uuid
from datetime import UTC, datetime, timedelta
from itertools import islice
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.constants import (
    MSG_ERROR_TRACKING_NOT_AVAILABLE,
    MSG_ERROR_TRACKING_TEMPORARILY_UNAVAILABLE,
    MSG_NO_ORGANISATION,
    MSG_UNEXPECTED_ERROR_OCCURRED_WHILE,
)
from modulo.api.db_error_handling import handle_db_errors, raise_session_contract_error
from modulo.api.dependencies import get_db_session, require_feature, require_permission, require_system_permission
from modulo.api.models.error import (
    ErrorEventInput,
    ErrorEventListResponse,
    ErrorGroupDetail,
    ErrorGroupResult,
    ErrorGroupUpdate,
    ErrorIngestRequest,
    ErrorIngestResponse,
    ErrorListResponse,
    SchedulerStarvationResponse,
    SessionKeyResponse,
)
from modulo.auth.dependencies import get_current_tenant_user
from modulo.auth.jwt import TenantPrincipal
from modulo.core.audit_coverage import audited, audited_system, bind_audit_org
from modulo.core.error_tracking import ErrorIngestionService, SessionKeyStore
from modulo.db.crud.error_tracking import (
    count_error_events_by_group,
    count_error_groups,
    get_error_events_by_group,
    get_error_group,
    get_error_groups,
    get_scheduler_starvation_pipelines,
    update_error_group,
)
from modulo.db.models.error_event import ErrorEvent
from modulo.db.models.error_group import ErrorGroup
from modulo.db.models.organisation import SYSTEM_ORG_ID
from modulo.db.rls import set_rls_org
from modulo.settings import Settings, get_settings

_CODE_ERRORS_RESOLVE = "errors.resolve"
_CODE_ERRORS_RESOLVE_INSTANCE = "errors.resolve_instance"
_CODE_ERRORS_INGEST_ERRORS = "errors.ingest_errors"
_CODE_ERRORS_INGEST_ERRORS_PUBLIC = "errors.ingest_errors_public"

_CODE_ERRORS_LIST_ERROR_GROUPS = "errors.list_error_groups"
_CODE_ERRORS_GET_ERROR_GROUP_DETAIL = "errors.get_error_group_detail"
_CODE_ERRORS_PATCH_ERROR_GROUP = "errors.patch_error_group"
_CODE_ERRORS_LIST_ERROR_EVENTS = "errors.list_error_events"

# Scheduler-starvation surfacing (FAR-604). Pending runs blocked on a capacity
# cap carry a RAW marker in ``runs.error_code`` (``error_codes.LEGACY_ALIASES``
# maps them to the dotted capacity.org / capacity.pipeline presentation codes).
# The same raw pair is what the stale-run sweep / dispatcher reconcile key on —
# the canonical ``CAPACITY_MARKERS`` (imported by the crud-layer detection).
_STARVATION_THRESHOLD_MINUTES = 10


_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/errors", tags=["errors"])

# Module-level singletons (lazy-initialised)
_service = ErrorIngestionService()
_key_store: SessionKeyStore | None = None

# Public ingest rate limiter and daily cap (in-memory, no Redis)
#
# Both maps are IP-keyed and live on the same UNAUTHENTICATED route, so both must
# be hard-bounded or a flood of one-off client IPs grows them forever.
# ``_public_rate_limit`` is a plain dict (not a defaultdict): every read is an
# explicit ``.get()`` and every write an explicit assignment, so a stray read
# can never silently grow the map past its bound.
_public_rate_limit: dict[str, list[float]] = {}  # IP -> list of request timestamps
_public_daily_event_count: dict[str, dict[str, int]] = {}  # IP -> {YYYY-MM-DD: count}

# Rate-limit window (1 request per 60 s per IP) and a hard bound on the number
# of tracked client IPs. This route is UNAUTHENTICATED, so without the bound the
# IP-keyed map grows forever: a key is only pruned when that same IP is seen
# again, so one-off client IPs accumulate their timestamp lists indefinitely (a
# memory-exhaustion DoS). The pa-identity rotation limiter shares this
# window/dict-of-timestamps shape but caps requests per client, not the number
# of tracked clients (only the demo-floor limiter in
# ``api/middleware/rate_limiter.py`` bounds its key set), so this key bound is
# new for this limiter.
_PUBLIC_RATE_LIMIT_WINDOW_SECONDS = 60.0
_MAX_TRACKED_PUBLIC_CLIENTS = 10_000

# The stale-key sweep runs on the per-request admission path, so one sweep
# inspects at most this many keys rather than the whole map. The front of the
# map holds the least-recently-touched keys, which under normal traffic are the
# idle (stale) ones, so the oldest batch is the highest-yield region to scan.
# It is a BEST-EFFORT reclaim, not an exact one: ``_touch_*`` is also called on
# the rejection path (to spare an actively-limited client from LRU eviction), so
# a key's touch time can be more recent than its newest stored timestamp and a
# stale key that was rate-limited shortly before going quiet can sit behind a
# still-in-window key. The hard bound does NOT depend on the sweep — the
# ``_evict_least_recently_used_*`` backstop guarantees it; the sweep only reduces
# how often that backstop has to drop an in-window client.
_PUBLIC_SWEEP_BATCH = 256
# At-capacity is an expected steady state under a unique-IP flood, so log the
# warning at most once per interval rather than on every request (log-flood
# guard); the ``_warn_*_at_capacity`` helpers compare against these timestamps.
_PUBLIC_CAPACITY_WARNING_INTERVAL_SECONDS = 300.0
_public_rate_limit_capacity_warning_at: float = 0.0
_public_daily_capacity_warning_at: float = 0.0

# Daily-cap retention window (48 h) and a hard bound on the number of tracked
# client IPs for ``_public_daily_event_count`` — bound by the same bounded-sweep
# + LRU pattern as the rate limiter, because the pre-fix code pruned only on the
# SUCCESS path and left an entry behind for every one-off IP that failed later.
_PUBLIC_DAILY_CAP_RETENTION_HOURS = 48
_MAX_TRACKED_PUBLIC_DAILY_CLIENTS = 10_000

# System / no-tenant sentinel org (SYSTEM_ORG_ID) is imported from
# modulo.db.models.organisation at module top — the single canonical
# definition shared with the admin listing filter and migration tooling
# (FAR-1505). Do NOT re-type the nil-UUID literal or re-alias it here.

# Breadcrumbs are persisted inside ``context_json`` under this key (PRD §8.25
# lists breadcrumbs as part of the event context payload).
BREADCRUMBS_CONTEXT_KEY = "breadcrumbs"


def _prepare_event_data(event: ErrorEventInput) -> dict[str, Any]:
    """Dump an ingest event, folding breadcrumbs into ``context_json``.

    The SDK sends breadcrumbs as a top-level field (capped at 50 by the
    ``ErrorEventInput`` validator), but the storage contract places them inside
    ``context_json`` (PRD §8.25). Without folding, ``model_dump`` would drop
    them and the breadcrumb trail would never reach the detail view.
    """
    data = event.model_dump(exclude={"breadcrumbs"})
    if event.breadcrumbs:
        context = dict(data.get("context_json") or {})
        context[BREADCRUMBS_CONTEXT_KEY] = event.breadcrumbs
        data["context_json"] = context
    return data


def _sweep_stale_public_rate_limit_clients(window_start: float) -> int:
    """Best-effort eviction of stale client IPs from the front of the LRU order.

    A client with no in-window request can never have tripped the limiter, so
    its key carries no rate-limiting information and is safe to drop. Only the
    first :data:`_PUBLIC_SWEEP_BATCH` keys are materialised (``islice``), so the
    per-request cost is O(batch) rather than O(tracked clients). This is a
    best-effort reclaim — see the ``_PUBLIC_SWEEP_BATCH`` note for why a stale
    key can sit behind an in-window one; the hard bound is the LRU backstop's
    job, not the sweep's. Returns the number of keys evicted.
    """
    stale = [
        ip
        for ip in islice(_public_rate_limit, _PUBLIC_SWEEP_BATCH)
        if not any(t > window_start for t in _public_rate_limit[ip])
    ]
    for ip in stale:
        del _public_rate_limit[ip]
    return len(stale)


def _touch_public_rate_limit_client(client_ip: str, timestamps: list[float]) -> None:
    """Store ``timestamps`` for ``client_ip``, marking it most-recently-used.

    Re-inserting an existing key moves it to the end of the dict's insertion
    order, so ``_evict_least_recently_used_public_clients`` evicts the client
    that has gone longest without a request rather than the one tracked longest.
    """
    _public_rate_limit.pop(client_ip, None)
    _public_rate_limit[client_ip] = timestamps


def _evict_least_recently_used_public_clients(limit: int) -> None:
    """Evict least-recently-used client IPs until at most ``limit`` remain.

    Backstop for the case the sweep cannot help with: every tracked client still
    holds an in-window timestamp, yet the map is at its bound. Eviction is
    best-effort LRU: a client that keeps being rejected is re-touched on every
    request and so stays most-recently-used, making it the last to be evicted.
    The hard bound is absolute, so once at capacity a client that has gone quiet
    can still be dropped and later admitted fresh.
    """
    while len(_public_rate_limit) > limit:
        del _public_rate_limit[next(iter(_public_rate_limit))]


def _rate_limit_exceeded_detail() -> str:
    """Client-facing 429 message, derived from the window so it cannot drift."""
    return f"Rate limit exceeded. Max 1 request per {_PUBLIC_RATE_LIMIT_WINDOW_SECONDS:g} seconds."


def _warn_public_rate_limit_at_capacity(now: float) -> None:
    """Warn that the rate limiter is evicting at capacity, at most once per interval."""
    global _public_rate_limit_capacity_warning_at
    if now - _public_rate_limit_capacity_warning_at < _PUBLIC_CAPACITY_WARNING_INTERVAL_SECONDS:
        return
    _public_rate_limit_capacity_warning_at = now
    _log.warning(
        "public_error_ingest: rate-limiter at capacity (%d tracked clients); evicting least-recently-used",
        _MAX_TRACKED_PUBLIC_CLIENTS,
    )


def _check_public_rate_limit(client_ip: str, now: float) -> None:
    """Record a public-ingest attempt, raising 429 if the client is over the limit.

    Allows at most one request per :data:`_PUBLIC_RATE_LIMIT_WINDOW_SECONDS`.
    Rejection also refreshes the client's LRU position, so a client that is
    actively being limited is evicted last by the hard bound — best-effort, see
    :func:`_evict_least_recently_used_public_clients` for the caveat.
    """
    window_start = now - _PUBLIC_RATE_LIMIT_WINDOW_SECONDS
    timestamps = [t for t in _public_rate_limit.get(client_ip, ()) if t > window_start]
    if timestamps:
        _touch_public_rate_limit_client(client_ip, timestamps)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=_rate_limit_exceeded_detail(),
        )
    # Keep the tracked-IP map hard-bounded before admitting a new key: sweep
    # idle IPs first (bounded), then evict the least-recently-used keys if the
    # sweep could not bring the map under the cap (all tracked clients are still
    # in-window). Evict down to one below the cap so the incoming key fits.
    if client_ip not in _public_rate_limit and len(_public_rate_limit) >= _MAX_TRACKED_PUBLIC_CLIENTS:
        _sweep_stale_public_rate_limit_clients(window_start)
        if len(_public_rate_limit) >= _MAX_TRACKED_PUBLIC_CLIENTS:
            _warn_public_rate_limit_at_capacity(now)
            _evict_least_recently_used_public_clients(_MAX_TRACKED_PUBLIC_CLIENTS - 1)
    timestamps.append(now)
    _touch_public_rate_limit_client(client_ip, timestamps)


def _public_daily_window_start(now: datetime | None = None) -> str:
    """Oldest date string still inside the daily-cap retention window."""
    return ((now or datetime.now(UTC)) - timedelta(hours=_PUBLIC_DAILY_CAP_RETENTION_HOURS)).strftime("%Y-%m-%d")


def _sweep_stale_public_daily_clients(threshold: str) -> int:
    """Best-effort eviction of stale daily-cap clients from the front of the LRU order.

    Mirrors :func:`_sweep_stale_public_rate_limit_clients`: materialise only the
    first :data:`_PUBLIC_SWEEP_BATCH` keys, drop clients with no counter dated
    at/after ``threshold``, and prune stale dated entries from the clients
    retained. Best-effort for the same reason as its rate-limit sibling; the
    LRU backstop, not this sweep, is the hard bound. Returns the count evicted.
    """
    to_evict = []
    for ip in islice(_public_daily_event_count, _PUBLIC_SWEEP_BATCH):
        days = _public_daily_event_count[ip]
        for date_str in [key for key in days if key < threshold]:
            del days[date_str]
        if not days:
            to_evict.append(ip)
    for ip in to_evict:
        del _public_daily_event_count[ip]
    return len(to_evict)


def _touch_public_daily_client(client_ip: str, days: dict[str, int]) -> None:
    """Store ``days`` for ``client_ip``, marking it most-recently-used."""
    _public_daily_event_count.pop(client_ip, None)
    _public_daily_event_count[client_ip] = days


def _evict_least_recently_used_public_daily_clients(limit: int) -> None:
    """Evict least-recently-used client IPs until at most ``limit`` remain."""
    while len(_public_daily_event_count) > limit:
        del _public_daily_event_count[next(iter(_public_daily_event_count))]


def _warn_public_daily_cap_at_capacity(now: float) -> None:
    """Warn that the daily-cap map is evicting at capacity, at most once per interval."""
    global _public_daily_capacity_warning_at
    if now - _public_daily_capacity_warning_at < _PUBLIC_CAPACITY_WARNING_INTERVAL_SECONDS:
        return
    _public_daily_capacity_warning_at = now
    _log.warning(
        "public_error_ingest: daily-cap at capacity (%d tracked clients); evicting least-recently-used",
        _MAX_TRACKED_PUBLIC_DAILY_CLIENTS,
    )


def _admit_public_daily_client(client_ip: str, now: float) -> dict[str, int]:
    """Return ``client_ip``'s daily-cap counters, keeping the map hard-bounded.

    Same pattern as :func:`_check_public_rate_limit`: a NEW key is admitted only
    after idle clients are swept (bounded) and, if the sweep could not bring the
    map under the cap, least-recently-used keys are evicted. Re-touching on every
    admitted request keeps an actively-seen client most-recently-used
    (best-effort LRU), and stale dated counters are dropped so a long-lived
    client's entry stays inside the retention window.
    """
    days = _public_daily_event_count.get(client_ip)
    if days is None:
        if len(_public_daily_event_count) >= _MAX_TRACKED_PUBLIC_DAILY_CLIENTS:
            _sweep_stale_public_daily_clients(_public_daily_window_start())
            if len(_public_daily_event_count) >= _MAX_TRACKED_PUBLIC_DAILY_CLIENTS:
                _warn_public_daily_cap_at_capacity(now)
                _evict_least_recently_used_public_daily_clients(_MAX_TRACKED_PUBLIC_DAILY_CLIENTS - 1)
        days = {}
    else:
        threshold = _public_daily_window_start()
        for date_str in [key for key in days if key < threshold]:
            del days[date_str]
    _touch_public_daily_client(client_ip, days)
    return days


def _get_key_store(settings: Settings | None = None) -> SessionKeyStore:
    global _key_store
    if _key_store is None:
        resolved = settings or get_settings()
        redis_client: Any = None
        if resolved.redis_url:
            try:
                from redis.asyncio import Redis

                redis_client = Redis.from_url(resolved.redis_url, decode_responses=False)
            except Exception:
                _log.warning("error_tracking.redis_unavailable — falling back to in-memory key store", exc_info=True)
        _key_store = SessionKeyStore(redis_client=redis_client)
    return _key_store


# Ingestion routes are intentionally NOT gated behind require_feature("error_tracking"):
# SDK error collection stays free on the community tier (recording is free-tier per PRD §8.25);
# only the read/dashboard/management routes are team-gated.


@router.post(
    "/session-key",
    response_model=SessionKeyResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[
        Depends(
            audited(
                "error_session_key_created",
                "error_session_key",
                principal_dep=get_current_tenant_user,
                fail_closed=True,
            ),
            scope="function",  # NOSONAR python:S930 - valid FastAPI Depends() kwarg; bundled signature is stale
        )
    ],
)
@handle_db_errors("errors.create_session_key")
async def create_session_key(
    principal: TenantPrincipal = require_permission(_CODE_ERRORS_RESOLVE),
) -> dict[str, Any]:
    """Generate a per-session HMAC key for signing error ingest requests.

    The key is stored for 1 hour and identified by the authenticated account.
    Include it as the ``X-Modulo-Error-Token`` header on ``/ingest`` requests.
    """
    store = _get_key_store()
    account_id = str(principal.account_id)
    key = await store.generate_key(account_id)
    return {"key": key, "expires_in_seconds": 3600}


# FAR-1538 ingest-volume decision: ACCEPT the audit event, do not baseline-exempt.
# One event per REQUEST: the browser batches on a 5s flush timer AND is
# rate-limited to 10 requests/minute per authenticated session, so a quiet client
# writes nothing. Exempting would drop coverage and need a baseline edit.
@router.post(
    "/ingest",
    response_model=ErrorIngestResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(audited("error_events_ingested", "error_event", principal_dep=get_current_tenant_user))],
)
@handle_db_errors(_CODE_ERRORS_INGEST_ERRORS)
async def ingest_errors(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_ERRORS_RESOLVE),
) -> dict[str, Any]:
    """Ingest one or more error events.

    * Body signed via ``X-Modulo-Error-Token`` header (HMAC-SHA256 of raw body).
    * Obtain a key via ``POST /api/v1/errors/session-key`` first.
    * Rate-limited to 10 requests/minute per authenticated session.
    """
    raw_body = await request.body()
    signature = request.headers.get("X-Modulo-Error-Token", "")

    if not signature:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-Modulo-Error-Token header",
        )

    store = _get_key_store()
    account_id = str(principal.account_id)
    if not await store.verify_hmac(account_id, raw_body, signature):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid HMAC signature",
        )

    try:
        data: dict[str, Any] = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Invalid JSON body",
        ) from exc

    try:
        ingest_request = ErrorIngestRequest(**data)
    except Exception as exc:
        _log.exception(_CODE_ERRORS_INGEST_ERRORS)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    events_data = [_prepare_event_data(e) for e in ingest_request.events]
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            org_id = principal.organisation_id
            if org_id is None:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Authenticated principal has no organisation",
                )
            results = await _service.ingest_batch(session, org_id, events_data)
    except HTTPException:
        raise
    except ProgrammingError as exc:
        _log.exception(_CODE_ERRORS_INGEST_ERRORS)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_ERROR_TRACKING_NOT_AVAILABLE,
        ) from exc
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, "errors.ingest_errors")
        _log.exception(_CODE_ERRORS_INGEST_ERRORS)
        _log.warning("error_tracking.db_error", extra={"org_id": str(principal.organisation_id)})
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MSG_ERROR_TRACKING_TEMPORARILY_UNAVAILABLE,
        ) from exc
    except Exception as exc:
        _log.exception("error_tracking.ingest_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR_OCCURRED_WHILE,
        ) from exc

    return {"results": [ErrorGroupResult(**r) for r in results]}


# FAR-1516: unauthenticated public ingress — no principal exists, so the
# actor-less variant records a SYSTEM actor under the system sentinel org (the
# same sentinel this route RLS-pins its event writes to: there is no tenant).
@router.post(
    "/ingest/public",
    response_model=ErrorIngestResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(audited_system("error_ingest_public", "error_event", actor_source="unauthenticated"))],
)
@handle_db_errors(_CODE_ERRORS_INGEST_ERRORS_PUBLIC)
async def ingest_errors_public(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Unauthenticated error ingest endpoint for frontend events.

    * No HMAC signing required.
    * Only accepts events with ``source == 'frontend'`` and ``level != 'critical'``.
    * Rate-limited to 1 request per 60 seconds per IP.
    * Daily cap of 100 events per IP.
    * Max request body size 10,000 bytes.
    * Events are stored in a dedicated orphan-org partition: the ingest
      transaction is RLS-pinned to a nil-UUID organisation row (seeded by
      migration 0172) that tenant sessions can never see (org-only RLS
      policies), so unattributed frontend errors never leak across tenancy.
    * A future cleanup job will prune events older than 48 hours (TTL).
    """
    # FAR-1516: this endpoint has no tenant by design — attribute the audit
    # event to the system sentinel org so the attempt (413, rate-limit, cap
    # and all) is still recorded.
    bind_audit_org(request, SYSTEM_ORG_ID)
    client_ip = request.client.host if request.client else "unknown"

    # Body size check
    raw_body = await request.body()
    if len(raw_body) > 10000:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail="Request body exceeds 10,000 bytes",
        )

    # Rate limit: 1 request per 60 seconds per IP
    now = _time.time()
    _check_public_rate_limit(client_ip, now)

    # Parse body
    try:
        data: dict[str, Any] = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Invalid JSON body",
        ) from exc

    try:
        ingest_request = ErrorIngestRequest(**data)
    except Exception as exc:
        _log.exception(_CODE_ERRORS_INGEST_ERRORS_PUBLIC)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    # Filter events: only frontend source, reject critical level
    valid_events = [
        event for event in ingest_request.events if event.source == "frontend" and event.level != "critical"
    ]

    if not valid_events:
        return {"results": []}

    # Daily cap: 100 events per IP
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    ip_counts = _admit_public_daily_client(client_ip, now)
    today_count = ip_counts.get(today, 0)
    if today_count + len(valid_events) > 100:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Daily cap exceeded. Max 100 events per IP per day.",
        )

    events_data = [_prepare_event_data(e) for e in valid_events]
    try:
        async with session.begin():
            # Pre-auth route (FAR-457 pattern): error_events/error_groups are
            # OrgScoped (org-only RLS), so the INSERTs below would fail the
            # policy's WITH CHECK when ``app.organisation_id`` is unset — and
            # ``ingest_batch`` swallows per-event errors (logged server-side),
            # which previously yielded a false-success 201 with an empty
            # results list and nothing persisted. Pin the transaction to the
            # system sentinel org (SYSTEM_ORG_ID — a real organisations row
            # seeded by migration 0172, satisfying the error_events FK) so the
            # writes pass WITH CHECK and the dedup/group lookups partition to
            # the sentinel rows exactly as their explicit ``organisation_id``
            # predicates intend.
            await set_rls_org(session, SYSTEM_ORG_ID)
            results = await _service.ingest_batch(session, SYSTEM_ORG_ID, events_data)
    except ProgrammingError as exc:
        _log.exception(_CODE_ERRORS_INGEST_ERRORS_PUBLIC)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_ERROR_TRACKING_NOT_AVAILABLE,
        ) from exc
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, "errors.ingest_errors_public")
        _log.exception(_CODE_ERRORS_INGEST_ERRORS_PUBLIC)
        _log.warning("error_tracking.public_ingest_db_error", extra={"ip": client_ip})
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MSG_ERROR_TRACKING_TEMPORARILY_UNAVAILABLE,
        ) from exc
    except Exception as exc:
        _log.exception("error_tracking.public_ingest_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR_OCCURRED_WHILE,
        ) from exc

    if not results:
        # ingest_batch swallows per-event failures (FK/RLS regressions,
        # malformed rows): a 201 with zero results would be a false success —
        # the client must learn persistence failed.
        _log.error(
            "error_tracking.public_ingest_not_persisted",
            extra={"ip": client_ip, "submitted": len(valid_events)},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error ingestion failed; no events could be persisted",
        )

    # Update daily cap count after successful ingest
    ip_counts[today] = today_count + len(valid_events)

    _log.info("public_error_ingest ip=%s count=%d", client_ip, len(valid_events))

    return {"results": [ErrorGroupResult(**r) for r in results]}


# ---------------------------------------------------------------------------
# Error dashboard — list / detail / update / events
# ---------------------------------------------------------------------------


def _serialize_error_group_summary(g: ErrorGroup, sample_event: ErrorEvent | None = None) -> dict[str, Any]:
    return {
        "id": str(g.id),
        "fingerprint": g.fingerprint,
        "status": g.status,
        "level_peak": g.level_peak,
        "count": g.count,
        "first_seen": g.first_seen.isoformat() if g.first_seen else "",
        "last_seen": g.last_seen.isoformat() if g.last_seen else "",
        "sample_message": sample_event.message if sample_event else "",
    }


def _serialize_error_event_detail(e: ErrorEvent) -> dict[str, Any]:
    context = e.context_json or {}
    return {
        "id": str(e.id),
        "level": e.level,
        "message": e.message,
        "stacktrace": e.stacktrace,
        "context_json": e.context_json,
        "source": e.source,
        "environment": e.environment,
        "version": e.version,
        "breadcrumbs": context.get(BREADCRUMBS_CONTEXT_KEY),
        "created_at": e.created_at.isoformat() if e.created_at else "",
    }


async def _fetch_sample_event(session: AsyncSession, org_id: uuid.UUID, group: ErrorGroup) -> ErrorEvent | None:
    if group.sample_event_id is None:
        return None
    result = await session.execute(
        select(ErrorEvent).where(
            ErrorEvent.organisation_id == org_id,
            ErrorEvent.id == group.sample_event_id,
        )
    )
    return result.scalar_one_or_none()


# ---------------------------------------------------------------------------
# Shared read bodies (FAR-1547).
#
# Each helper owns ONE transaction: it pins the RLS org context and runs every
# read inside ``session.begin()`` (an error-tracking read outside the pin would
# run with no org context and silently see zero rows). The caller decides WHICH
# org the transaction is pinned to — the tenant route passes
# ``principal.organisation_id``, the instance-scope route passes the
# SYSTEM_ORG_ID sentinel — so the sentinel-partition read cannot drift from the
# tenant read, and RLS policies pass on both. The DB-error mapping stays with
# the route (``handle_db_errors`` / the route-local arms).
# ---------------------------------------------------------------------------


async def _list_error_groups_body(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    status_filter: str | None,
    level: str | None,
    source: str | None,
    environment: str | None,
    search: str | None,
    limit: int,
    offset: int,
) -> dict[str, Any]:
    async with session.begin():
        await set_rls_org(session, org_id)
        groups = await get_error_groups(
            session=session,
            org_id=org_id,
            status=status_filter,
            level=level,
            source=source,
            environment=environment,
            search=search,
            limit=limit,
            offset=offset,
        )
        total = await count_error_groups(
            session=session,
            org_id=org_id,
            status=status_filter,
            level=level,
            source=source,
            environment=environment,
            search=search,
        )

        sample_ids = [g.sample_event_id for g in groups if g.sample_event_id is not None]
        if sample_ids:
            result = await session.execute(
                select(ErrorEvent).where(
                    ErrorEvent.organisation_id == org_id,
                    ErrorEvent.id.in_(sample_ids),
                )
            )
            sample_events = {event.id: event for event in result.scalars().all()}
        else:
            sample_events = {}

        items = []
        for g in groups:
            sample = sample_events.get(g.sample_event_id) if g.sample_event_id else None
            items.append(_serialize_error_group_summary(g, sample))

    return {"items": items, "total": total, "limit": limit, "offset": offset}


async def _error_group_detail_body(
    session: AsyncSession,
    org_id: uuid.UUID,
    error_id: uuid.UUID,
) -> dict[str, Any]:
    async with session.begin():
        await set_rls_org(session, org_id)
        group = await get_error_group(session=session, org_id=org_id, group_id=error_id)
        if group is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Error group not found")
        sample = await _fetch_sample_event(session, org_id, group)
    return {
        "id": str(group.id),
        "fingerprint": group.fingerprint,
        "status": group.status,
        "level_peak": group.level_peak,
        "count": group.count,
        "first_seen": group.first_seen.isoformat() if group.first_seen else "",
        "last_seen": group.last_seen.isoformat() if group.last_seen else "",
        "sample_event": _serialize_error_event_detail(sample) if sample else None,
        "assigned_to": str(group.assigned_to) if group.assigned_to else None,
    }


async def _error_group_events_body(
    session: AsyncSession,
    org_id: uuid.UUID,
    error_id: uuid.UUID,
    *,
    limit: int,
    offset: int,
) -> dict[str, Any]:
    async with session.begin():
        await set_rls_org(session, org_id)
        group = await get_error_group(session=session, org_id=org_id, group_id=error_id)
        if group is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Error group not found")

        events = await get_error_events_by_group(
            session=session, org_id=org_id, group_id=error_id, limit=limit, offset=offset
        )
        total = await count_error_events_by_group(session=session, org_id=org_id, group_id=error_id)

    items = [_serialize_error_event_detail(e) for e in events]
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.get("", response_model=ErrorListResponse, dependencies=[require_feature("error_tracking")])
@handle_db_errors(_CODE_ERRORS_LIST_ERROR_GROUPS)
async def list_error_groups(
    status_filter: str | None = Query(None, alias="status"),
    level: str | None = Query(None),
    source: str | None = Query(None),
    environment: str | None = Query(None),
    search: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_ERRORS_RESOLVE),
) -> dict[str, Any]:
    org_id = principal.organisation_id
    if org_id is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=MSG_NO_ORGANISATION)

    try:
        return await _list_error_groups_body(
            session,
            org_id,
            status_filter=status_filter,
            level=level,
            source=source,
            environment=environment,
            search=search,
            limit=limit,
            offset=offset,
        )
    except ProgrammingError as exc:
        _log.exception(_CODE_ERRORS_LIST_ERROR_GROUPS)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_ERROR_TRACKING_NOT_AVAILABLE,
        ) from exc
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, _CODE_ERRORS_LIST_ERROR_GROUPS)
        _log.exception(_CODE_ERRORS_LIST_ERROR_GROUPS)
        _log.warning("error_tracking.list_groups_db_error")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MSG_ERROR_TRACKING_TEMPORARILY_UNAVAILABLE,
        ) from exc
    except Exception as exc:
        _log.exception("error_tracking.list_groups_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR_OCCURRED_WHILE,
        ) from exc


@router.get(
    "/scheduler-starvation",
    response_model=SchedulerStarvationResponse,
    dependencies=[require_feature("error_tracking")],
)
@handle_db_errors("errors.scheduler_starvation")
async def get_scheduler_starvation(
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_ERRORS_RESOLVE),
) -> dict[str, Any]:
    """Scheduler-starvation condition for the error dashboard (FAR-604).

    Pipelines having unstarted runs (``status='pending'``, ``started_at IS
    NULL``) with a capacity-marker ``error_code`` whose age anchor is older
    than the starvation threshold (10 minutes). Declared BEFORE the
    ``/{error_id}`` routes so the static path wins routing. Pre-terminal
    pending runs never produce error events — the dashboard otherwise keys off
    ingested errors from terminal failures — so a pipeline stuck at its
    concurrency cap is invisible without this surface. Each item carries the
    pipeline id/name, the count of starved pending runs, and the oldest run's
    age anchor + age. The age anchor is the run's EARLIEST trigger-event
    receipt (``MIN(trigger_events.received_at)``, falling back to
    ``created_at`` when the run has no trigger event): a coalescing
    re-delivery refreshes the pending run's ``created_at`` on the dispatcher's
    short re-dispatch cadence, so a ``created_at``-keyed age would reset every
    cycle and make a days-long wedge look minutes old. The aggregate is one
    row per starved pipeline (bounded by the org's pipeline count), so no
    pagination and ``total`` is the exact item count. Detection (the SQL
    aggregate) lives in the crud layer
    (:func:`modulo.db.crud.error_tracking.get_scheduler_starvation_pipelines`)
    — the route keeps auth, RLS pinning and serialization only; the
    ``handle_db_errors`` decorator owns the DB-error mapping (it re-raises
    ``HTTPException`` untouched).
    """
    org_id = principal.organisation_id
    if org_id is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=MSG_NO_ORGANISATION)

    threshold = datetime.now(UTC) - timedelta(minutes=_STARVATION_THRESHOLD_MINUTES)
    async with session.begin():
        await set_rls_org(session, org_id)
        rows = await get_scheduler_starvation_pipelines(session=session, org_id=org_id, threshold=threshold)

    now = datetime.now(UTC)
    items = []
    for row in rows:
        oldest = row.oldest_created_at
        items.append(
            {
                "pipeline_id": str(row.pipeline_id),
                "pipeline_name": row.pipeline_name,
                "pending_count": int(row.pending_count),
                "oldest_created_at": oldest.isoformat() if oldest else "",
                "oldest_age_minutes": round((now - oldest).total_seconds() / 60, 1) if oldest else 0.0,
            }
        )
    return {"items": items, "total": len(items), "threshold_minutes": _STARVATION_THRESHOLD_MINUTES}


# ---------------------------------------------------------------------------
# Instance-scope read — the SYSTEM_ORG_ID sentinel partition (FAR-1547)
#
# The public error-ingest path and (per FAR-1484) org-less backend ERRORs
# write instance-level / unattributed rows into the SYSTEM_ORG_ID partition,
# which every tenant-scoped route is structurally unable to see (they all
# pin ``principal.organisation_id``). These read routes expose that partition
# to a SYSTEM ADMIN ONLY: the gate is the route-level
# ``require_system_permission("errors.resolve_instance")`` dependency, which
# is evaluated BEFORE the handler, so a tenant principal — with or without a
# forged parameter — is refused 403 rather than silently served its own
# rows. Each handler pins the transaction to the sentinel org (the helper
# owns ``set_rls_org`` inside ``session.begin()``) so the org-only RLS
# policies pass on the read.
#
# Deliberately READ-ONLY: no instance-scope PATCH. Mutating another tenant's
# (or the instance's) error groups is out of scope for this view.
#
# The ``/instance`` list route is declared BEFORE the ``/{error_id}`` routes:
# it is a single path segment and would otherwise be swallowed by the UUID
# path converter (422), exactly like ``/scheduler-starvation`` above.
# ---------------------------------------------------------------------------


@router.get(
    "/instance",
    response_model=ErrorListResponse,
    dependencies=[
        require_system_permission(_CODE_ERRORS_RESOLVE_INSTANCE),
        require_feature("error_tracking"),
    ],
)
@handle_db_errors("errors.list_instance_error_groups")
async def list_instance_error_groups(
    status_filter: str | None = Query(None, alias="status"),
    level: str | None = Query(None),
    source: str | None = Query(None),
    environment: str | None = Query(None),
    search: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """List the instance-scope (unattributed) error groups — system admin only."""
    return await _list_error_groups_body(
        session,
        SYSTEM_ORG_ID,
        status_filter=status_filter,
        level=level,
        source=source,
        environment=environment,
        search=search,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/instance/{error_id}",
    response_model=ErrorGroupDetail,
    dependencies=[
        require_system_permission(_CODE_ERRORS_RESOLVE_INSTANCE),
        require_feature("error_tracking"),
    ],
)
@handle_db_errors("errors.get_instance_error_group_detail")
async def get_instance_error_group_detail(
    error_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Read one instance-scope error group — system admin only."""
    return await _error_group_detail_body(session, SYSTEM_ORG_ID, error_id)


@router.get(
    "/instance/{error_id}/events",
    response_model=ErrorEventListResponse,
    dependencies=[
        require_system_permission(_CODE_ERRORS_RESOLVE_INSTANCE),
        require_feature("error_tracking"),
    ],
)
@handle_db_errors("errors.list_instance_error_events")
async def list_instance_error_events(
    error_id: uuid.UUID,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """List the raw events of one instance-scope error group — system admin only."""
    return await _error_group_events_body(session, SYSTEM_ORG_ID, error_id, limit=limit, offset=offset)


@router.get("/{error_id}", response_model=ErrorGroupDetail, dependencies=[require_feature("error_tracking")])
@handle_db_errors(_CODE_ERRORS_GET_ERROR_GROUP_DETAIL)
async def get_error_group_detail(
    error_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_ERRORS_RESOLVE),
) -> dict[str, Any]:
    org_id = principal.organisation_id
    if org_id is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=MSG_NO_ORGANISATION)

    try:
        return await _error_group_detail_body(session, org_id, error_id)
    except HTTPException:
        raise
    except ProgrammingError as exc:
        _log.exception(_CODE_ERRORS_GET_ERROR_GROUP_DETAIL)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_ERROR_TRACKING_NOT_AVAILABLE,
        ) from exc
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, _CODE_ERRORS_GET_ERROR_GROUP_DETAIL)
        _log.exception(_CODE_ERRORS_GET_ERROR_GROUP_DETAIL)
        _log.warning("error_tracking.get_group_detail_db_error")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MSG_ERROR_TRACKING_TEMPORARILY_UNAVAILABLE,
        ) from exc
    except Exception as exc:
        _log.exception("error_tracking.get_group_detail_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR_OCCURRED_WHILE,
        ) from exc


@router.patch(
    "/{error_id}",
    response_model=ErrorGroupDetail,
    dependencies=[
        Depends(audited("error_group_updated", "error_group", principal_dep=get_current_tenant_user)),
        require_feature("error_tracking"),
    ],
)
@handle_db_errors(_CODE_ERRORS_PATCH_ERROR_GROUP)
async def patch_error_group(
    error_id: uuid.UUID,
    req: ErrorGroupUpdate,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_ERRORS_RESOLVE),
) -> dict[str, Any]:
    org_id = principal.organisation_id
    if org_id is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=MSG_NO_ORGANISATION)

    try:
        async with session.begin():
            await set_rls_org(session, org_id)
            try:
                group = await update_error_group(
                    session=session,
                    org_id=org_id,
                    group_id=error_id,
                    status=req.status,
                    assigned_to=uuid.UUID(req.assigned_to) if req.assigned_to else None,
                )
            except ValueError as exc:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

            sample = await _fetch_sample_event(session, org_id, group)
    except HTTPException:
        raise
    except ProgrammingError as exc:
        _log.exception(_CODE_ERRORS_PATCH_ERROR_GROUP)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_ERROR_TRACKING_NOT_AVAILABLE,
        ) from exc
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, _CODE_ERRORS_PATCH_ERROR_GROUP)
        _log.exception(_CODE_ERRORS_PATCH_ERROR_GROUP)
        _log.warning("error_tracking.patch_group_db_error")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MSG_ERROR_TRACKING_TEMPORARILY_UNAVAILABLE,
        ) from exc
    except Exception as exc:
        _log.exception("error_tracking.patch_group_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR_OCCURRED_WHILE,
        ) from exc

    return {
        "id": str(group.id),
        "fingerprint": group.fingerprint,
        "status": group.status,
        "level_peak": group.level_peak,
        "count": group.count,
        "first_seen": group.first_seen.isoformat() if group.first_seen else "",
        "last_seen": group.last_seen.isoformat() if group.last_seen else "",
        "sample_event": _serialize_error_event_detail(sample) if sample else None,
        "assigned_to": str(group.assigned_to) if group.assigned_to else None,
    }


@router.get(
    "/{error_id}/events",
    response_model=ErrorEventListResponse,
    dependencies=[require_feature("error_tracking")],
)
@handle_db_errors(_CODE_ERRORS_LIST_ERROR_EVENTS)
async def list_error_events(
    error_id: uuid.UUID,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_ERRORS_RESOLVE),
) -> dict[str, Any]:
    org_id = principal.organisation_id
    if org_id is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=MSG_NO_ORGANISATION)

    try:
        return await _error_group_events_body(session, org_id, error_id, limit=limit, offset=offset)
    except HTTPException:
        raise
    except ProgrammingError as exc:
        _log.exception(_CODE_ERRORS_LIST_ERROR_EVENTS)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_ERROR_TRACKING_NOT_AVAILABLE,
        ) from exc
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, _CODE_ERRORS_LIST_ERROR_EVENTS)
        _log.exception(_CODE_ERRORS_LIST_ERROR_EVENTS)
        _log.warning("error_tracking.list_events_db_error")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MSG_ERROR_TRACKING_TEMPORARILY_UNAVAILABLE,
        ) from exc
    except Exception as exc:
        _log.exception("error_tracking.list_events_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR_OCCURRED_WHILE,
        ) from exc
