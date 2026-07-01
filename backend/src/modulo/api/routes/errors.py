"""Error tracking API — session-key generation, event ingestion, and dashboard."""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.dependencies import get_db_session
from modulo.api.models.error import (
    ErrorEventListResponse,
    ErrorGroupDetail,
    ErrorGroupResult,
    ErrorGroupUpdate,
    ErrorIngestRequest,
    ErrorIngestResponse,
    ErrorListResponse,
    SessionKeyResponse,
)
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
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
from modulo.db.rls import set_rls_org
from modulo.settings import Settings, get_settings

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/errors", tags=["errors"])

# Module-level singletons (lazy-initialised)
_service = ErrorIngestionService()
_key_store: SessionKeyStore | None = None


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
                _log.warning("error_tracking.redis_unavailable — falling back to in-memory key store")
        _key_store = SessionKeyStore(redis_client=redis_client)
    return _key_store


@router.post("/session-key", response_model=SessionKeyResponse, status_code=status.HTTP_201_CREATED)
async def create_session_key(
    principal: AuthenticatedPrincipal = Depends(get_current_user),
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
async def ingest_errors(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
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
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Invalid JSON body",
        ) from exc

    try:
        ingest_request = ErrorIngestRequest(**data)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    events_data = [e.model_dump(exclude={"breadcrumbs"}) for e in ingest_request.events]
    async with session.begin():
        await set_rls_org(session, principal.organisation_id)
        org_id = principal.organisation_id
        if org_id is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Authenticated principal has no organisation",
            )
        results = await _service.ingest_batch(session, org_id, events_data)

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
    return {
        "id": str(e.id),
        "level": e.level,
        "message": e.message,
        "stacktrace": e.stacktrace,
        "context_json": e.context_json,
        "source": e.source,
        "environment": e.environment,
        "version": e.version,
        "breadcrumbs": None,
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


@router.get("", response_model=ErrorListResponse)
async def list_error_groups(
    status_filter: str | None = Query(None, alias="status"),
    level: str | None = Query(None),
    source: str | None = Query(None),
    environment: str | None = Query(None),
    search: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> dict[str, Any]:
    org_id = principal.organisation_id
    if org_id is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No organisation")

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

        items = []
        for g in groups:
            sample = await _fetch_sample_event(session, org_id, g)
            items.append(_serialize_error_group_summary(g, sample))

    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.get("/{error_id}", response_model=ErrorGroupDetail)
async def get_error_group_detail(
    error_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> dict[str, Any]:
    org_id = principal.organisation_id
    if org_id is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No organisation")

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


@router.patch("/{error_id}", response_model=ErrorGroupDetail)
async def patch_error_group(
    error_id: uuid.UUID,
    body: ErrorGroupUpdate,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> dict[str, Any]:
    org_id = principal.organisation_id
    if org_id is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No organisation")

    async with session.begin():
        await set_rls_org(session, org_id)
        try:
            group = await update_error_group(
                session=session,
                org_id=org_id,
                group_id=error_id,
                status=body.status,
                assigned_to=uuid.UUID(body.assigned_to) if body.assigned_to else None,
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

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


@router.get("/{error_id}/events", response_model=ErrorEventListResponse)
async def list_error_events(
    error_id: uuid.UUID,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> dict[str, Any]:
    org_id = principal.organisation_id
    if org_id is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No organisation")

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
