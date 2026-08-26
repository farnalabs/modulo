"""Admin run data retention routes (FAR-427) — list / export / purge.

Complementary to the existing `/api/v1/admin/runs/retention` (config) and
`/api/v1/admin/runs/purge` (age-based) routes. Those only touch the ``runs``
rows; this router adds the endpoints that let an operator (a) EXPORT old runs to
a file and (b) CLEAR them down including their LangGraph checkpoints and related
per-run rows, to reclaim DB volume.

Authz: org admins operate within their own org (RLS filters ``organisation_id``);
system admins may operate across all orgs, optionally narrowed by an
``organisation_id`` parameter. The feature is gated on ``admin_run_retention``
(team tier) for consistency with the existing run-retention surface.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import status as http_status  # nosemgrep: loopvar-shadows-import
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.constants import MSG_UNEXPECTED_ERROR
from modulo.api.dependencies import (
    get_db_session,
    require_feature,
    require_system_or_org_admin,
)
from modulo.auth.jwt import TenantPrincipal
from modulo.db.crud.run_retention import (
    iter_run_export,
    list_retention_candidates,
    purge_terminal_runs,
)
from modulo.db.rls import set_rls_org

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/admin/run-retention", tags=["admin-run-retention"])

_PERMISSION = "run_retention.manage"


class RetentionCandidate(BaseModel):
    id: str
    created_at: str | None
    status: str
    pipeline_id: str
    thread_id: str
    estimated_bytes: int


class CandidatesResponse(BaseModel):
    runs: list[RetentionCandidate]
    total_count: int
    total_estimated_bytes: int
    terminal_total: int = 0
    terminal_estimated_bytes: int = 0


class RetentionFilter(BaseModel):
    date_from: datetime | None = None
    date_to: datetime | None = None
    pipeline_id: uuid.UUID | None = None
    status: str | None = None
    organisation_id: uuid.UUID | None = None


class ExportRequest(RetentionFilter):
    pass


class PurgeRequest(RetentionFilter):
    confirm: bool = False


class PurgeResponse(BaseModel):
    purged_runs: int
    purged_checkpoints: int
    freed_estimated_bytes: int


def _resolve_org_id(principal: TenantPrincipal, organisation_id: uuid.UUID | None) -> uuid.UUID | None:
    """Resolve the effective org scope for the request.

    A system admin may target any org via ``organisation_id`` (None = all orgs,
    cross-tenant). An org admin is always bound to their own organisation — a
    mismatched ``organisation_id`` is rejected rather than silently ignored.
    """

    if principal.is_system_admin:
        return organisation_id
    if organisation_id is not None and organisation_id != principal.organisation_id:
        raise HTTPException(
            status_code=http_status.HTTP_403_FORBIDDEN,
            detail="Org admins may only operate on their own organisation",
        )
    return principal.organisation_id


async def _run_scoped(session: AsyncSession, org_id: uuid.UUID | None) -> None:
    """Establish RLS scope. ``None`` (system admin, all orgs) skips RLS."""

    await set_rls_org(session, org_id)


@router.get("/candidates", dependencies=[require_feature("admin_run_retention")])
async def candidates(
    session: Annotated[AsyncSession, Depends(get_db_session)],
    date_from: Annotated[datetime | None, Query()] = None,
    date_to: Annotated[datetime | None, Query()] = None,
    pipeline_id: Annotated[uuid.UUID | None, Query()] = None,
    status: Annotated[str | None, Query()] = None,
    organisation_id: Annotated[uuid.UUID | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=5000)] = 500,
    offset: Annotated[int, Query(ge=0)] = 0,
    principal: TenantPrincipal = require_system_or_org_admin(_PERMISSION),
) -> CandidatesResponse:
    """List runs matching the filter set, with an estimated per-run byte size.

    Returns every matching run (including non-terminal ones — the UI shows
    terminal-only as purge-able), the total match count, an estimated total
    reclaimable byte count across ALL matches, and — most importantly for the
    purge confirm dialog — a terminal-only ``terminal_total`` and
    ``terminal_estimated_bytes``. The purge deletes *every* matching terminal run
    (unbounded), so the client must derive its confirm count and reclaimable
    figure from these server-side terminal totals, never from the page-capped
    candidate list it happens to hold.
    """

    org_id = _resolve_org_id(principal, organisation_id)
    try:
        async with session.begin():
            await _run_scoped(session, org_id)
            result = await list_retention_candidates(
                session,
                org_id=org_id,
                date_from=date_from,
                date_to=date_to,
                pipeline_id=pipeline_id,
                status=status,
                limit=limit,
                offset=offset,
            )
    except ProgrammingError:
        _log.exception("run_retention.candidates.programming_error")
        raise HTTPException(
            status_code=http_status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    except SQLAlchemyError:
        _log.exception("run_retention.candidates.db_error")
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database temporarily unavailable.",
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception("run_retention.candidates.unexpected_error")
        raise HTTPException(
            status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None

    return CandidatesResponse(**result)


@router.post("/export", dependencies=[require_feature("admin_run_retention")])
async def export(
    req: ExportRequest,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    principal: TenantPrincipal = require_system_or_org_admin(_PERMISSION),
) -> StreamingResponse:
    """Stream a JSONL file of the matching runs (metadata + full outputs).

    Memory-safe: runs are streamed in pages and each run's checkpoint summary is
    aggregated in a batch, so nothing accumulates. The response is a download
    with a ``Content-Disposition`` attachment header.
    """

    org_id = _resolve_org_id(principal, req.organisation_id)
    filename = "run-retention-export.ndjson"

    async def _stream() -> AsyncIterator[str]:
        async with session.begin():
            await _run_scoped(session, org_id)
            async for line in iter_run_export(
                session,
                org_id=org_id,
                date_from=req.date_from,
                date_to=req.date_to,
                pipeline_id=req.pipeline_id,
                status=req.status,
            ):
                yield line

    return StreamingResponse(
        _stream(),
        media_type="application/x-ndjson",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/purge", dependencies=[require_feature("admin_run_retention")])
async def purge(
    req: PurgeRequest,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    principal: TenantPrincipal = require_system_or_org_admin(_PERMISSION),
) -> PurgeResponse:
    """Delete terminal runs matching the filter set, cascading to checkpoints.

    Requires ``confirm: true``. Only terminal-status runs are ever deleted; the
    purge is batched (500 runs per SAVEPOINT), transactional, and idempotent.
    Never deletes a pending/running/awaiting_human/claimed run. On success an
    audit event is appended to the affected org's chain.
    """

    if not req.confirm:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail="Purge requires an explicit `confirm: true` body value.",
        )

    org_id = _resolve_org_id(principal, req.organisation_id)
    try:
        async with session.begin():
            await _run_scoped(session, org_id)
            result = await purge_terminal_runs(
                session,
                org_id=org_id,
                date_from=req.date_from,
                date_to=req.date_to,
                pipeline_id=req.pipeline_id,
                _status=req.status,
            )

            if org_id is not None:
                from modulo.core.audit_logger import append_audit_event

                await append_audit_event(
                    session,
                    org_id=org_id,
                    event_type="run_retention_purge",
                    actor_user_id=principal.account_id,
                    resource_type="run",
                    payload_json={
                        "purged_runs": result["purged_runs"],
                        "purged_checkpoints": result["purged_checkpoints"],
                        "freed_estimated_bytes": result["freed_estimated_bytes"],
                        "date_from": str(req.date_from) if req.date_from else None,
                        "date_to": str(req.date_to) if req.date_to else None,
                        "pipeline_id": str(req.pipeline_id) if req.pipeline_id else None,
                        "status": req.status,
                    },
                )
    except IntegrityError:
        _log.exception("run_retention.purge.integrity_error")
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="Resource conflict. The purge could not be completed.",
        ) from None
    except ProgrammingError:
        _log.exception("run_retention.purge.programming_error")
        raise HTTPException(
            status_code=http_status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    except SQLAlchemyError:
        _log.exception("run_retention.purge.db_error")
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database temporarily unavailable.",
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception("run_retention.purge.unexpected_error")
        raise HTTPException(
            status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_UNEXPECTED_ERROR,
        ) from None

    _log.info(
        "run_retention.purged",
        extra={
            "org_id": str(org_id) if org_id else None,
            "purged_runs": result["purged_runs"],
            "purged_checkpoints": result["purged_checkpoints"],
            "freed_estimated_bytes": result["freed_estimated_bytes"],
        },
    )
    return PurgeResponse(**result)
