"""Admin housekeeping routes — list and delete cleanup candidates."""

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.dependencies import get_db_session
from modulo.auth.dependencies import get_current_tenant_user
from modulo.auth.jwt import TenantPrincipal
from modulo.core.housekeeping import ENTITY_MODEL_MAP, scan_all
from modulo.db.rls import set_rls_org

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/admin/housekeeping", tags=["admin-housekeeping"])


def _require_admin(principal: TenantPrincipal) -> None:
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
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> HousekeepingScanResponse:
    _require_admin(principal)
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            results = await scan_all(session, principal.organisation_id)
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
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> CleanupResponse:
    _require_admin(principal)
    deleted_count = 0
    errors: list[dict[str, str]] = []

    grouped: dict[str, list[str]] = {}
    for item in req.items:
        grouped.setdefault(item.entity_type, []).append(item.id)

    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)

            for entity_type, ids in grouped.items():
                model_cls = ENTITY_MODEL_MAP.get(entity_type)
                if model_cls is None:
                    errors.append({"entity_type": entity_type, "error": f"Unknown entity type: {entity_type}"})
                    continue

                try:
                    async with session.begin_nested():
                        for eid in ids:
                            try:
                                obj = await session.get(model_cls, eid)  # type: ignore[func-returns-value]
                                if obj is not None:
                                    await session.delete(obj)
                                    deleted_count += 1
                            except Exception as exc:
                                errors.append({"id": eid, "entity_type": entity_type, "error": str(exc)})
                except IntegrityError:
                    _log.warning("IntegrityError cleaning up %s group — skipping", entity_type)
                    errors.append({"entity_type": entity_type, "error": "Foreign key constraint violation"})
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
