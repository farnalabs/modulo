"""Error tracking API — session-key generation and event ingestion."""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.dependencies import get_db_session
from modulo.api.models.error import ErrorGroupResult, ErrorIngestRequest, ErrorIngestResponse, SessionKeyResponse
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.core.error_tracking import ErrorIngestionService, SessionKeyStore
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
