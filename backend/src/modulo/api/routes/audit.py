"""Audit chain verification, browsing, and export API."""

from __future__ import annotations

import csv
import io
import json
import logging
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any
from uuid import UUID as _UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.responses import StreamingResponse

from modulo.api.constants import MSG_FEATURE_NOT_AVAILABLE, MSG_INTERNAL_SERVER_ERROR
from modulo.api.db_error_handling import (
    handle_db_errors,
    prime_stream,
    raise_session_contract_error,
    reemit_primed_stream,
)
from modulo.api.dependencies import (
    get_db_session,
    get_or_create_engine,
    require_feature,
    require_permission,
)
from modulo.auth.jwt import TenantPrincipal
from modulo.core.audit_logger import (
    SCAN_CSV_COLUMNS,
    SCAN_KEYSET_PAGE_SIZE,
    export_chain,
    get_audit_events_batch,
    list_audit_events,
    stream_export_chain,
    verify_chain,
)
from modulo.core.evidence_pack import (
    EvidenceSigningError,
    build_evidence_pack,
    configured_public_key_pem,
    key_fingerprint,
)
from modulo.db.rls import set_rls_org
from modulo.settings import Settings, get_settings

_CODE_AUDIT_MANAGE = "audit.manage"
_MSG_DATABASE_CONNECTION_FAILED_PLEASE = "Database connection failed. Please try again."
_SCAN_NDJSON_MEDIA_TYPE = "application/x-ndjson"

#: Upper bound on the events embedded in one evidence pack - the same 10,000
#: ceiling ``verify_chain`` applies. Beyond it the pack is marked truncated
#: (honest: the offline verifier sees the flag and the signed event_count).
_EVIDENCE_PACK_MAX_EVENTS = 10000

_DESC_ACTOR_USER_ID = "Filter by actor user ID"
_DESC_START_DATE = "Filter by start date (ISO 8601)"
_DESC_END_DATE = "Filter by end date (ISO 8601)"
_DESC_PRINCIPAL_KIND = "Filter by acting principal kind (human/service)"
_PRINCIPAL_KIND_PATTERN = "^(human|service)$"


_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/admin/audit", tags=["audit"])


class BatchDetailRequest(BaseModel):
    event_ids: list[str]


@router.get("", dependencies=[require_feature("audit_viewer")])
@handle_db_errors("audit.list_audit_events_endpoint")
async def list_audit_events_endpoint(
    cursor: str | None = Query(None, max_length=256, description="Cursor: JSON {c:created_at, i:id}"),
    limit: int = Query(50, ge=1, le=200, description="Number of events per page"),
    event_type: str | None = Query(None, max_length=64, description="Filter by event type (action_type)"),
    actor_user_id: str | None = Query(None, max_length=64, alias="user_id", description=_DESC_ACTOR_USER_ID),
    resource_type: str | None = Query(
        None,
        max_length=64,
        alias="entity_type",
        description="Filter by resource type (entity_type)",
    ),
    from_date: datetime | None = Query(None, alias="from_date", description=_DESC_START_DATE),
    to_date: datetime | None = Query(None, alias="to_date", description=_DESC_END_DATE),
    principal_kind: str | None = Query(None, pattern=_PRINCIPAL_KIND_PATTERN, description=_DESC_PRINCIPAL_KIND),
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_AUDIT_MANAGE),
) -> dict[str, object]:
    """List audit events with cursor pagination and filters.

    Supports filtering by event_type (action_type), user_id (actor_user_id),
    entity_type (resource_type), from_date, to_date, principal_kind.
    """
    actor_uid = None
    if actor_user_id:
        try:
            actor_uid = _UUID(actor_user_id)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"Invalid user_id format: {actor_user_id!r}. Must be a valid UUID.",
            ) from None
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            result = await list_audit_events(
                session,
                principal.organisation_id,
                cursor=cursor,
                limit=limit,
                event_type=event_type,
                actor_user_id=actor_uid,
                resource_type=resource_type,
                from_date=from_date,
                to_date=to_date,
                principal_kind=principal_kind,
            )
    except ProgrammingError:
        _log.exception("audit.list_audit_events_endpoint")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, "audit.list_audit_events_endpoint")
        _log.exception("list_audit_events: database error")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_CONNECTION_FAILED_PLEASE,
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception("Unexpected error in list_audit_events_endpoint")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None
    return result


# FAR-1472 exemption (read-only POST): reads a batch of audit events by id and
# writes nothing, so chaining an audit event per lookup would only add noise to
# the append-only chain. Deliberately left in audit_coverage_baseline.txt.
@router.post("/batch-detail", dependencies=[require_feature("audit_viewer")])
async def batch_detail_endpoint(
    req: BatchDetailRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_AUDIT_MANAGE),
) -> list[dict[str, object]]:
    """Return full details for a batch of audit event IDs."""
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            result = await get_audit_events_batch(
                session,
                principal.organisation_id,
                req.event_ids,
            )
    except ProgrammingError:
        _log.exception("audit.batch_detail_endpoint")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, "audit.batch_detail_endpoint")
        _log.exception("batch_detail: database error")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_CONNECTION_FAILED_PLEASE,
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception("Unexpected error in batch_detail_endpoint")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None
    return result


@router.get("/verify", dependencies=[require_feature("audit_viewer")])
@handle_db_errors("audit.verify_chain_endpoint")
async def verify_chain_endpoint(
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_AUDIT_MANAGE),
) -> dict[str, object]:
    """Verify the cryptographic integrity of the org's audit chain."""
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            result = await verify_chain(session, principal.organisation_id)
    except ProgrammingError:
        _log.exception("audit.verify_chain_endpoint")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, "audit.verify_chain_endpoint")
        _log.exception("verify_chain: database error")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_CONNECTION_FAILED_PLEASE,
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception("Unexpected error in verify_chain_endpoint")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None
    return result


@router.get("/export", dependencies=[require_feature("audit_viewer")])
async def export_chain_endpoint(
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=1000),
    event_type: str | None = Query(None, max_length=64, description="Filter by event type"),
    actor_user_id: str | None = Query(None, max_length=64, alias="user_id", description=_DESC_ACTOR_USER_ID),
    resource_type: str | None = Query(None, max_length=64, alias="entity_type", description="Filter by resource type"),
    from_date: datetime | None = Query(None, description=_DESC_START_DATE),
    to_date: datetime | None = Query(None, description=_DESC_END_DATE),
    principal_kind: str | None = Query(None, pattern=_PRINCIPAL_KIND_PATTERN, description=_DESC_PRINCIPAL_KIND),
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_AUDIT_MANAGE),
) -> dict[str, object]:
    """Export audit events as paginated JSON with optional filters."""
    actor_uid = None
    if actor_user_id:
        try:
            actor_uid = _UUID(actor_user_id)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"Invalid user_id format: {actor_user_id!r}. Must be a valid UUID.",
            ) from None
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            result = await export_chain(
                session,
                principal.organisation_id,
                page=page,
                page_size=page_size,
                event_type=event_type,
                actor_user_id=actor_uid,
                resource_type=resource_type,
                from_date=from_date,
                to_date=to_date,
                principal_kind=principal_kind,
            )
    except ProgrammingError:
        _log.exception("audit.export_chain_endpoint")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, "audit.export_chain_endpoint")
        _log.exception("export_chain: database error")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_CONNECTION_FAILED_PLEASE,
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception("Unexpected error in export_chain_endpoint")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None
    return result


def _audit_session_factory(settings: Settings) -> async_sessionmaker[AsyncSession]:
    """Dedicated sessionmaker over the EXISTING shared engine (autobegin=False)."""
    return async_sessionmaker(get_or_create_engine(settings), expire_on_commit=False, autobegin=False)


def _csv_stream_line(*, header: bool, item: dict[str, Any] | None = None) -> str:
    """One properly-quoted CSV line for the scan stream (header xor row)."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    if header:
        writer.writerow(list(SCAN_CSV_COLUMNS))
    elif item is not None:
        writer.writerow([item.get(name, "") for name in SCAN_CSV_COLUMNS])
    return buffer.getvalue()


async def _scan_body(
    stream: AsyncIterator[dict[str, Any]],
    *,
    first: dict[str, Any] | None,
    format: str,
) -> AsyncIterator[str]:
    """Chunk the primed scan generator into the wire format (NDJSON or CSV).

    A thin format adapter over the shared
    :func:`modulo.api.db_error_handling.reemit_primed_stream` — the
    first-item-then-the-rest loop lives in ONE place for all three streaming
    exports. ``first`` is the row already fetched by the route to surface
    pre-stream errors (validation/permission/database) as a proper HTTP status;
    it is re-emitted here so no event is lost.
    """
    if format == "csv":
        body = reemit_primed_stream(
            stream,
            first,
            lambda item: _csv_stream_line(header=False, item=item),
            header=_csv_stream_line(header=True),
        )
    else:
        body = reemit_primed_stream(stream, first, lambda item: json.dumps(item) + "\n")
    async for chunk in body:
        yield chunk


@router.get("/scan", dependencies=[require_feature("audit_viewer")])
async def scan_chain_endpoint(
    format: str = Query("json", pattern="^(json|csv)$"),
    event_type: str | None = Query(None, max_length=64, description="Filter by event type"),
    actor_user_id: str | None = Query(None, max_length=64, alias="user_id", description=_DESC_ACTOR_USER_ID),
    resource_type: str | None = Query(None, max_length=64, alias="entity_type", description="Filter by resource type"),
    from_date: datetime | None = Query(None, description=_DESC_START_DATE),
    to_date: datetime | None = Query(None, description=_DESC_END_DATE),
    principal_kind: str | None = Query(None, pattern=_PRINCIPAL_KIND_PATTERN, description=_DESC_PRINCIPAL_KIND),
    settings: Settings = Depends(get_settings),
    principal: TenantPrincipal = require_permission(_CODE_AUDIT_MANAGE),
) -> Response:
    """Server-side scan export: stream the WHOLE org audit chain in ONE response.

    The deferral companion to ``/export``: the same typed filters, org-scoped
    RLS and ``audit_viewer`` + ``audit.manage`` gates, but no ``page`` /
    ``page_size`` pagination — the server keyset-paginates internally over the
    stable ``(created_at, id)`` order in fixed batches, so memory stays bounded
    for any org size while the client receives the entire result as a single
    streaming body. ``format=json`` (default) is NDJSON (one audit event per
    line); ``format=csv`` is a Content-Disposition CSV attachment with the
    scan columns.
    """
    actor_uid = None
    if actor_user_id:
        try:
            actor_uid = _UUID(actor_user_id)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"Invalid user_id format: {actor_user_id!r}. Must be a valid UUID.",
            ) from None
    stream = stream_export_chain(
        factory=_audit_session_factory(settings),
        org_id=principal.organisation_id,
        event_type=event_type,
        actor_user_id=actor_uid,
        resource_type=resource_type,
        from_date=from_date,
        to_date=to_date,
        principal_kind=principal_kind,
        page_size=SCAN_KEYSET_PAGE_SIZE,
    )
    # Prime the generator so the RLS write and the first keyset page surface
    # pre-stream DB errors as a proper HTTP status BEFORE the response starts —
    # routed through the shared classifier (409/422/501/503/re-raise), not a
    # hand-rolled subset.
    first = await prime_stream(stream, "audit.scan_chain_endpoint")

    body = _scan_body(stream, first=first, format=format)
    if format == "csv":
        return StreamingResponse(
            body,
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="audit-scan.csv"'},
        )
    return StreamingResponse(body, media_type=_SCAN_NDJSON_MEDIA_TYPE)


async def _collect_pack_events(settings: Settings, org_id: _UUID) -> tuple[list[dict[str, Any]], bool]:
    """Collect the org's audit events for a pack, capped at the verify ceiling.

    Returns ``(events, truncated)``. Streaming keeps memory bounded for large
    orgs; the cap mirrors ``verify_chain``'s 10,000-event window and the
    ``truncated`` flag is carried into the signed manifest so an offline reader
    sees exactly how much of the chain the pack covers.
    """
    events: list[dict[str, Any]] = []
    truncated = False
    stream = stream_export_chain(
        factory=_audit_session_factory(settings), org_id=org_id, page_size=SCAN_KEYSET_PAGE_SIZE
    )
    async for event in stream:
        if len(events) >= _EVIDENCE_PACK_MAX_EVENTS:
            truncated = True
            break
        events.append(event)
    return events, truncated


@router.get("/evidence-pack", dependencies=[require_feature("audit_viewer")])
async def evidence_pack_endpoint(
    run_id: str | None = Query(None, max_length=128, description="Optional run id to name in the pack manifest"),
    settings: Settings = Depends(get_settings),
    principal: TenantPrincipal = require_permission(_CODE_AUDIT_MANAGE),
) -> dict[str, object]:
    """Build and sign the offline-verifiable evidence pack for the org's chain.

    The response is a self-contained artefact (signed manifest + Ed25519
    signature + published verification key + file bytes) that a third party can
    verify with nothing but this body and the pre-published public key.
    """
    try:
        events, truncated = await _collect_pack_events(settings, principal.organisation_id)
        pack = build_evidence_pack(
            organisation_id=str(principal.organisation_id),
            events=events,
            run_ids=[run_id] if run_id else [],
            truncated=truncated,
        )
    except EvidenceSigningError as exc:
        _log.warning("audit.evidence_pack_endpoint.signing_unavailable")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from None
    except ProgrammingError:
        _log.exception("audit.evidence_pack_endpoint")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, "audit.evidence_pack_endpoint")
        _log.exception("evidence_pack_endpoint: database error")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_CONNECTION_FAILED_PLEASE,
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception("Unexpected error in evidence_pack_endpoint")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None
    return pack


@router.get(
    "/verification-key",
    dependencies=[require_feature("audit_viewer"), require_permission(_CODE_AUDIT_MANAGE)],
)
async def verification_key_endpoint() -> dict[str, object]:
    """Publish the org's Ed25519 evidence verification key (public, not secret).

    An external auditor can fetch this once and verify any evidence pack the
    org exports; the key is also embedded in every pack as an artefact.
    """
    try:
        public_key_pem = configured_public_key_pem()
    except EvidenceSigningError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from None
    return {
        "algorithm": "ed25519",
        "public_key_pem": public_key_pem,
        "key_id": key_fingerprint(public_key_pem),
    }
