"""Admin housekeeping routes — list and delete cleanup candidates."""

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.dependencies import get_db_session
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.core.housekeeping import delete_housekeeping_items, scan_all
from modulo.db.rls import set_rls_org

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/admin/housekeeping", tags=["admin-housekeeping"])


def _require_admin(principal: AuthenticatedPrincipal) -> None:
    if principal.org_role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin role required",
        )


class CandidateItem(BaseModel):
    id: str
    name: str
    detail: str
    created_at: str | None = None
    entity_type: str = ""


class HousekeepingCategory(BaseModel):
    category: str
    label: str
    description: str
    candidates: list[CandidateItem]
    count: int


class HousekeepingScanResponse(BaseModel):
    categories: list[HousekeepingCategory]
    total_count: int


class CleanupItem(BaseModel):
    id: str
    entity_type: str


class CleanupRequest(BaseModel):
    items: list[CleanupItem]


class CleanupResponse(BaseModel):
    deleted_count: int
    errors: list[dict[str, str]]


@router.get("", response_model=HousekeepingScanResponse)
async def list_housekeeping(
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> HousekeepingScanResponse:
    _require_admin(principal)
    org_id = principal.organisation_id
    if org_id is None:
        return HousekeepingScanResponse(categories=[], total_count=0)

    try:
        async with session.begin():
            await set_rls_org(session, org_id)
            results = await scan_all(session, org_id)
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    except SQLAlchemyError:
        _log.exception("admin_housekeeping.list")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database temporarily unavailable.",
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception("admin_housekeeping.list")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        ) from None

    categories_list = [
        HousekeepingCategory(
            category=r.category,
            label=r.label,
            description=r.description,
            candidates=[
                CandidateItem(
                    id=c.id,
                    name=c.name,
                    detail=c.detail,
                    created_at=c.created_at,
                    entity_type=c.entity_type,
                )
                for c in r.candidates
            ],
            count=len(r.candidates),
        )
        for r in results
    ]
    total = sum(len(r.candidates) for r in results)
    return HousekeepingScanResponse(categories=categories_list, total_count=total)


@router.post("/cleanup", response_model=CleanupResponse)
async def perform_cleanup(
    req: CleanupRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> CleanupResponse:
    _require_admin(principal)
    org_id = principal.organisation_id
    if org_id is None:
        return CleanupResponse(deleted_count=0, errors=[])

    items_list = [{"entity_type": i.entity_type, "id": i.id} for i in req.items]

    try:
        async with session.begin():
            await set_rls_org(session, org_id)
            deleted_count, errors = await delete_housekeeping_items(session, org_id, items_list)
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    except SQLAlchemyError:
        _log.exception("admin_housekeeping.cleanup")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database temporarily unavailable.",
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception("admin_housekeeping.cleanup")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        ) from None

    return CleanupResponse(deleted_count=deleted_count, errors=errors)
