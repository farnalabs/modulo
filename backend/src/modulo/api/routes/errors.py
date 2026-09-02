"""Error tracking API — session-key generation, event ingestion, and dashboard."""

from __future__ import annotations

import json
import logging
import time as _time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.db_error_handling import handle_db_errors
from modulo.api.dependencies import get_db_session, require_feature, require_permission
from modulo.api.models.error import (
    ErrorEventInput,
    ErrorEventListResponse,
    ErrorGroupDetail,
    ErrorGroupResult,
    ErrorGroupUpdate,
    ErrorIngestRequest,
    ErrorIngestResponse,
    ErrorListResponse,
    SessionKeyResponse,
)
from modulo.auth.jwt import TenantPrincipal
from modulo.core.error_tracking import ErrorIngestionService, SessionKeyStore
from modulo.db.crud.error_tracking import (
    count_error_events_by_group,
    count_error_groups,
    get_error_events_by_group,
    get_error_group,
    get_error_groups,
    update_error_group,
)
from modulo.db.models.error_event import ErrorEvent
from modulo.db.models.error_group import ErrorGroup
from modulo.db.models.organisation import ORPHAN_ORG_ID as _ORPHAN_ORG_ID
from modulo.db.rls import set_rls_org
from modulo.settings import Settings, get_settings

_CODE_ERRORS_RESOLVE = "errors.resolve"
_CODE_ERRORS_INGEST_ERRORS = "errors.ingest_errors"
_MSG_ERROR_TRACKING_NOT_AVAILABLE = "Error tracking is not available. Run database migrations to enable it."
_MSG_ERROR_TRACKING_TEMPORARILY_UNAVAILABLE = "Error tracking is temporarily unavailable. Please try again."
_MSG_UNEXPECTED_ERROR_OCCURRED_WHILE = "An unexpected error occurred while processing your request."
_CODE_ERRORS_INGEST_ERRORS_PUBLIC = "errors.ingest_errors_public"
_MSG_NO_ORGANISATION = "No organisation"


_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/errors", tags=["errors"])

# Module-level singletons (lazy-initialised)
_service = ErrorIngestionService()
_key_store: SessionKeyStore | None = None

# Public ingest rate limiter and daily cap (in-memory, no Redis)
_public_rate_limit: dict[str, list[float]] = {}  # IP -> list of request timestamps
_public_daily_event_count: dict[str, dict[str, int]] = {}  # IP -> {YYYY-MM-DD: count}

# Orphan org ID for unauthenticated public ingest events — the shared
# sentinel constant lives on the Organisation model so the ingest path, the
# admin listing filter and migration tooling cannot drift apart. Re-exported
# here under the same name for backward compatibility.
ORPHAN_ORG_ID = _ORPHAN_ORG_ID

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


def _prune_stale_ip_counters() -> None:
    """Remove IP entries with no activity in the last 48 hours."""
    threshold = (datetime.now(UTC) - timedelta(hours=48)).strftime("%Y-%m-%d")
    stale_ips = []
    for ip, days in _public_daily_event_count.items():
        for date_str in list(days.keys()):
            if date_str < threshold:
                del days[date_str]
        if not days:
            stale_ips.append(ip)
    for ip in stale_ips:
        del _public_daily_event_count[ip]


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


@router.post("/session-key", response_model=SessionKeyResponse, status_code=status.HTTP_201_CREATED)
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


@router.post("/ingest", response_model=ErrorIngestResponse, status_code=status.HTTP_201_CREATED)
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
            detail=_MSG_ERROR_TRACKING_NOT_AVAILABLE,
        ) from exc
    except SQLAlchemyError as exc:
        _log.exception(_CODE_ERRORS_INGEST_ERRORS)
        _log.warning("error_tracking.db_error", extra={"org_id": str(principal.organisation_id)})
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_ERROR_TRACKING_TEMPORARILY_UNAVAILABLE,
        ) from exc
    except Exception as exc:
        _log.exception("error_tracking.ingest_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=_MSG_UNEXPECTED_ERROR_OCCURRED_WHILE,
        ) from exc

    return {"results": [ErrorGroupResult(**r) for r in results]}


@router.post("/ingest/public", response_model=ErrorIngestResponse, status_code=status.HTTP_201_CREATED)
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
      migration 0171) that tenant sessions can never see (org-only RLS
      policies), so unattributed frontend errors never leak across tenancy.
    * A future cleanup job will prune events older than 48 hours (TTL).
    """
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
    timestamps = _public_rate_limit.setdefault(client_ip, [])
    timestamps[:] = [t for t in timestamps if now - t < 60]
    if len(timestamps) >= 1:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded. Max 1 request per 60 seconds.",
        )
    timestamps.append(now)

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
    ip_counts = _public_daily_event_count.setdefault(client_ip, {})
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
            # orphan org (a real organisations row seeded by migration 0171,
            # satisfying the error_events FK) so the writes pass WITH CHECK
            # and the dedup/group lookups partition to the orphan rows
            # exactly as their explicit ``organisation_id`` predicates intend.
            await set_rls_org(session, ORPHAN_ORG_ID)
            results = await _service.ingest_batch(session, ORPHAN_ORG_ID, events_data)
    except ProgrammingError as exc:
        _log.exception(_CODE_ERRORS_INGEST_ERRORS_PUBLIC)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_ERROR_TRACKING_NOT_AVAILABLE,
        ) from exc
    except SQLAlchemyError as exc:
        _log.exception(_CODE_ERRORS_INGEST_ERRORS_PUBLIC)
        _log.warning("error_tracking.public_ingest_db_error", extra={"ip": client_ip})
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_ERROR_TRACKING_TEMPORARILY_UNAVAILABLE,
        ) from exc
    except Exception as exc:
        _log.exception("error_tracking.public_ingest_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=_MSG_UNEXPECTED_ERROR_OCCURRED_WHILE,
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
    _prune_stale_ip_counters()

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


@router.get("", response_model=ErrorListResponse, dependencies=[require_feature("error_tracking")])
@handle_db_errors("errors.list_error_groups")
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
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=_MSG_NO_ORGANISATION)

    try:
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
    except ProgrammingError as exc:
        _log.exception("errors.list_error_groups")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_ERROR_TRACKING_NOT_AVAILABLE,
        ) from exc
    except SQLAlchemyError as exc:
        _log.exception("errors.list_error_groups")
        _log.warning("error_tracking.list_groups_db_error")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_ERROR_TRACKING_TEMPORARILY_UNAVAILABLE,
        ) from exc
    except Exception as exc:
        _log.exception("error_tracking.list_groups_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=_MSG_UNEXPECTED_ERROR_OCCURRED_WHILE,
        ) from exc

    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.get("/{error_id}", response_model=ErrorGroupDetail, dependencies=[require_feature("error_tracking")])
@handle_db_errors("errors.get_error_group_detail")
async def get_error_group_detail(
    error_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_ERRORS_RESOLVE),
) -> dict[str, Any]:
    org_id = principal.organisation_id
    if org_id is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=_MSG_NO_ORGANISATION)

    try:
        async with session.begin():
            await set_rls_org(session, org_id)
            group = await get_error_group(session=session, org_id=org_id, group_id=error_id)
            if group is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Error group not found")
            sample = await _fetch_sample_event(session, org_id, group)
    except HTTPException:
        raise
    except ProgrammingError as exc:
        _log.exception("errors.get_error_group_detail")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_ERROR_TRACKING_NOT_AVAILABLE,
        ) from exc
    except SQLAlchemyError as exc:
        _log.exception("errors.get_error_group_detail")
        _log.warning("error_tracking.get_group_detail_db_error")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_ERROR_TRACKING_TEMPORARILY_UNAVAILABLE,
        ) from exc
    except Exception as exc:
        _log.exception("error_tracking.get_group_detail_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=_MSG_UNEXPECTED_ERROR_OCCURRED_WHILE,
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


@router.patch("/{error_id}", response_model=ErrorGroupDetail, dependencies=[require_feature("error_tracking")])
@handle_db_errors("errors.patch_error_group")
async def patch_error_group(
    error_id: uuid.UUID,
    req: ErrorGroupUpdate,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_ERRORS_RESOLVE),
) -> dict[str, Any]:
    org_id = principal.organisation_id
    if org_id is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=_MSG_NO_ORGANISATION)

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
        _log.exception("errors.patch_error_group")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_ERROR_TRACKING_NOT_AVAILABLE,
        ) from exc
    except SQLAlchemyError as exc:
        _log.exception("errors.patch_error_group")
        _log.warning("error_tracking.patch_group_db_error")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_ERROR_TRACKING_TEMPORARILY_UNAVAILABLE,
        ) from exc
    except Exception as exc:
        _log.exception("error_tracking.patch_group_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=_MSG_UNEXPECTED_ERROR_OCCURRED_WHILE,
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
@handle_db_errors("errors.list_error_events")
async def list_error_events(
    error_id: uuid.UUID,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_ERRORS_RESOLVE),
) -> dict[str, Any]:
    org_id = principal.organisation_id
    if org_id is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=_MSG_NO_ORGANISATION)

    try:
        async with session.begin():
            await set_rls_org(session, org_id)
            group = await get_error_group(session=session, org_id=org_id, group_id=error_id)
            if group is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Error group not found")

            events = await get_error_events_by_group(
                session=session, org_id=org_id, group_id=error_id, limit=limit, offset=offset
            )
            total = await count_error_events_by_group(session=session, org_id=org_id, group_id=error_id)
    except HTTPException:
        raise
    except ProgrammingError as exc:
        _log.exception("errors.list_error_events")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=_MSG_ERROR_TRACKING_NOT_AVAILABLE,
        ) from exc
    except SQLAlchemyError as exc:
        _log.exception("errors.list_error_events")
        _log.warning("error_tracking.list_events_db_error")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_ERROR_TRACKING_TEMPORARILY_UNAVAILABLE,
        ) from exc
    except Exception as exc:
        _log.exception("error_tracking.list_events_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=_MSG_UNEXPECTED_ERROR_OCCURRED_WHILE,
        ) from exc

    items = [_serialize_error_event_detail(e) for e in events]
    return {"items": items, "total": total, "limit": limit, "offset": offset}
