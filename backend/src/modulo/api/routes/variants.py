"""Variant group API — A/B test management endpoints."""

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.db_error_handling import handle_db_errors
from modulo.api.dependencies import get_db_session
from modulo.auth.dependencies import get_current_tenant_user
from modulo.auth.jwt import TenantPrincipal
from modulo.db.crud.variant_group import (
    check_pipeline_run_quota,
    create_variant_group,
    delete_variant_group,
    get_coverage_gaps,
    get_prompt_diffs,
    get_variant_group,
    list_variant_groups,
    run_variant_weighted,
    update_variant_group,
)
from modulo.db.rls import set_rls_org

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/variant-groups", tags=["variant-groups"])


class VariantDef(BaseModel):
    snapshot_id: str | uuid.UUID
    name: str
    weight: float = Field(default=1.0, ge=0)
    run_context_overrides: dict[str, Any] = Field(default_factory=dict)
    eval_definition_ids: list[str | uuid.UUID] = Field(default_factory=list)


class CreateVariantGroupRequest(BaseModel):
    pipeline_id: uuid.UUID
    name: str
    description: str | None = None
    variants: list[VariantDef] = Field(default_factory=list)
    selection_strategy: str = "weighted"
    max_concurrent_runs: int = 5
    degraded_evals: bool = False


class VariantGroupResponse(BaseModel):
    id: uuid.UUID
    pipeline_id: uuid.UUID
    name: str
    description: str | None
    variants: list[dict[str, Any]]
    selection_strategy: str
    run_count: int
    max_concurrent_runs: int
    degraded_evals: bool
    created_at: str
    updated_at: str


class RunVariantResponse(BaseModel):
    run_id: uuid.UUID
    variant_name: str
    merged_payload: dict[str, Any]


class CoverageGap(BaseModel):
    variant: dict[str, Any]
    missing_evals: list[str]


class PromptDiffEntry(BaseModel):
    base_variant: dict[str, Any]
    variant: dict[str, Any]
    agent_diffs: list[dict[str, Any]]


class RunVariantRequest(BaseModel):
    input_payload: dict[str, Any] = Field(default_factory=dict)


def _variant_to_response(group: Any) -> dict[str, Any]:
    return {
        "id": group.id,
        "pipeline_id": group.pipeline_id,
        "name": group.name,
        "description": group.description,
        "variants": group.variants if isinstance(group.variants, list) else [],
        "selection_strategy": group.selection_strategy,
        "run_count": group.run_count or 0,
        "max_concurrent_runs": group.max_concurrent_runs,
        "degraded_evals": group.degraded_evals,
        "created_at": group.created_at.isoformat() if group.created_at else "",
        "updated_at": group.updated_at.isoformat() if group.updated_at else "",
    }


@handle_db_errors("variants.create_group")
@router.post("", response_model=VariantGroupResponse, status_code=status.HTTP_201_CREATED)
async def create_group(
    req: CreateVariantGroupRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> dict[str, Any]:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            group = await create_variant_group(
                session,
                org_id=principal.organisation_id,
                pipeline_id=req.pipeline_id,
                name=req.name,
                variants=[v.model_dump() for v in req.variants],
                description=req.description,
                selection_strategy=req.selection_strategy,
                max_concurrent_runs=req.max_concurrent_runs,
                degraded_evals=req.degraded_evals,
            )
    except IntegrityError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Resource conflict. The referenced pipeline may not exist.",
        ) from None
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    except SQLAlchemyError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database error occurred. Please try again.",
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception("Unexpected error in variant group endpoint")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred. Please try again.",
        ) from None

    return _variant_to_response(group)


@handle_db_errors("variants.list_groups")
@router.get("", response_model=list[VariantGroupResponse])
async def list_groups(
    pipeline_id: uuid.UUID | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> list[dict[str, Any]]:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            items, _total = await list_variant_groups(session, pipeline_id=pipeline_id, page=page, page_size=page_size)
    except IntegrityError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A resource with this value already exists",
        ) from None
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    except SQLAlchemyError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database error occurred. Please try again.",
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception("Unexpected error in variant group list endpoint")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred. Please try again.",
        ) from None
    return [_variant_to_response(g) for g in items]


@handle_db_errors("variants.get_group")
@router.get("/{group_id}", response_model=VariantGroupResponse)
async def get_group(
    group_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> dict[str, Any]:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            group = await get_variant_group(session, group_id)
    except IntegrityError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A resource with this value already exists",
        ) from None
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    except SQLAlchemyError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database error occurred. Please try again.",
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception("Unexpected error in variant group endpoint")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred. Please try again.",
        ) from None
    if group is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Variant group not found")
    return _variant_to_response(group)


@handle_db_errors("variants.update_group")
@router.put("/{group_id}", response_model=VariantGroupResponse)
async def update_group(
    group_id: uuid.UUID,
    req: CreateVariantGroupRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> dict[str, Any]:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            group = await update_variant_group(
                session,
                group_id,
                name=req.name,
                description=req.description,
                variants=[v.model_dump() for v in req.variants],
                selection_strategy=req.selection_strategy,
                max_concurrent_runs=req.max_concurrent_runs,
                degraded_evals=req.degraded_evals,
            )
    except IntegrityError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Resource conflict. The referenced pipeline may not exist.",
        ) from None
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    except SQLAlchemyError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database error occurred. Please try again.",
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception("Unexpected error in variant group endpoint")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred. Please try again.",
        ) from None
    if group is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Variant group not found")
    return _variant_to_response(group)


@handle_db_errors("variants.delete_group")
@router.delete("/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_group(
    group_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> None:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            deleted = await delete_variant_group(session, group_id)
    except IntegrityError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot delete variant group — it is referenced by existing runs.",
        ) from None
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    except SQLAlchemyError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database error occurred. Please try again.",
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception("Unexpected error in variant group delete endpoint")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred. Please try again.",
        ) from None
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Variant group not found")
    return


@handle_db_errors("variants.run_variant")
@router.post("/{group_id}/run", response_model=RunVariantResponse)
async def run_variant(
    group_id: uuid.UUID,
    req: RunVariantRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> dict[str, Any]:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            group = await get_variant_group(session, group_id)
            if group is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Variant group not found",
                )

            if not group.variants:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="Variant group has no variants configured",
                )

            if not await check_pipeline_run_quota(session, group):
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="Pipeline concurrent run quota exceeded",
                )

            result = await run_variant_weighted(
                session,
                org_id=principal.organisation_id,
                group=group,
                input_payload=req.input_payload,
                account_id=principal.account_id,
            )
    except IntegrityError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Resource conflict. The referenced pipeline or snapshot may not exist.",
        ) from None
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    except SQLAlchemyError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database error occurred. Please try again.",
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception("Unexpected error in variant group run endpoint")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred. Please try again.",
        ) from None

    if result is None:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Pipeline concurrent run quota exceeded",
        ) from None

    return {
        "run_id": result["run_id"],
        "variant_name": result["variant"].get("name", "unknown"),
        "merged_payload": result["merged_payload"],
    }


@handle_db_errors("variants.coverage_gaps")
@router.get("/{group_id}/coverage-gaps", response_model=list[CoverageGap])
async def coverage_gaps(
    group_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> list[dict[str, Any]]:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            group = await get_variant_group(session, group_id)
            if group is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Variant group not found",
                )
            gaps = await get_coverage_gaps(session, group)
    except IntegrityError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A resource with this value already exists",
        ) from None
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    except SQLAlchemyError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database error occurred. Please try again.",
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception("Unexpected error in variant group coverage-gaps endpoint")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred. Please try again.",
        ) from None
    return gaps


@handle_db_errors("variants.prompt_diffs")
@router.get("/{group_id}/prompt-diffs", response_model=list[PromptDiffEntry])
async def prompt_diffs(
    group_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> list[dict[str, Any]]:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            group = await get_variant_group(session, group_id)
            if group is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Variant group not found",
                )
            diffs = await get_prompt_diffs(session, group)
    except IntegrityError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A resource with this value already exists",
        ) from None
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    except SQLAlchemyError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database error occurred. Please try again.",
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception("Unexpected error in variant group prompt-diffs endpoint")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred. Please try again.",
        ) from None
    return diffs
