"""Variant group API — A/B test management endpoints."""

import logging
import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.constants import MSG_FEATURE_NOT_AVAILABLE, MSG_RESOURCE_ALREADY_EXISTS
from modulo.api.db_error_handling import handle_db_errors
from modulo.api.dependencies import get_db_session, require_permission
from modulo.api.routes.runs import _mask_output_value
from modulo.auth.jwt import TenantPrincipal
from modulo.db.crud.run import get_run
from modulo.db.crud.variant_group import (
    check_pipeline_run_quota,
    create_variant_group,
    get_batch_compare,
    get_coverage_gaps,
    get_prompt_diffs,
    get_variant_group,
    has_pipeline_default_evals,
    list_variant_groups,
    restore_variant_group,
    run_variant_batch,
    run_variant_weighted,
    soft_delete_variant_group,
    update_variant_group,
    validate_batch_ownership,
)
from modulo.db.rls import set_rls_org, set_rls_user_context

_CODE_VARIANTS_CREATE_GROUP = "variants.create_group"
_MSG_DATABASE_ERROR_OCCURRED_PLEASE = "Database error occurred. Please try again."
_MSG_UNEXPECTED_ERROR_VARIANT_GROUP = "Unexpected error in variant group endpoint"
_MSG_UNEXPECTED_ERROR_OCCURRED_PLEASE = "An unexpected error occurred. Please try again."
_CODE_VARIANT_LIST = "variant.list"
_CODE_VARIANTS_LIST_GROUPS = "variants.list_groups"
_CODE_VARIANTS_GET_GROUP = "variants.get_group"
_MSG_VARIANT_GROUP_NOT_FOUND = "Variant group not found"
_CODE_VARIANTS_UPDATE_GROUP = "variants.update_group"
_CODE_VARIANTS_DELETE_GROUP = "variants.delete_group"
_CODE_VARIANTS_RESTORE_GROUP = "variants.restore_group"
_CODE_VARIANTS_RUN_VARIANT = "variants.run_variant"
_CODE_VARIANTS_RUN_VARIANT_BATCH = "variants.run_variant_batch"
_CODE_VARIANTS_COVERAGE_GAPS = "variants.coverage_gaps"
_CODE_VARIANTS_PROMPT_DIFFS = "variants.prompt_diffs"
_CODE_VARIANTS_BATCH_COMPARE = "variants.batch_compare"


_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/variant-groups", tags=["variant-groups"])


class VariantDef(BaseModel):
    # Stable persisted id (FAR-332 3b) — minted by the frontend on Duplicate and
    # round-tripped through variant-group CRUD. Optional so legacy payloads
    # without it keep validating; a fresh id is minted server-side when absent
    # at create time so every persisted variant carries one.
    id: str | uuid.UUID | None = None
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
    selection_strategy: Literal["weighted", "single"] = "weighted"
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


class RunVariantBatchResponse(BaseModel):
    runs: list[RunVariantResponse]
    count: int
    batch_id: uuid.UUID
    # Eval-coverage signal (FAR-332 3g) — False when the pipeline has no default
    # evals, so the frontend can show "no evals → cost/diff only". A warn, not a
    # hard block: the batch still fires.
    has_evals: bool = True


class BatchRunCompare(BaseModel):
    run_id: uuid.UUID
    run_number: int
    status: str
    variant_id: str | None = None
    variant_name: str
    snapshot_id: uuid.UUID | None = None
    run_context_overrides: dict[str, Any] = Field(default_factory=dict)
    eval_pass_rate: float | None = None
    eval_count: int = 0
    total_cost_usd: Any = None
    total_tokens: int | None = None
    created_at: Any = None
    completed_at: Any = None
    override_diff: dict[str, Any] = Field(default_factory=dict)


class BatchCompareResponse(BaseModel):
    batch_id: uuid.UUID
    has_evals: bool = True
    runs: list[BatchRunCompare] = Field(default_factory=list)


class CoverageGap(BaseModel):
    variant: dict[str, Any]
    missing_evals: list[str]


class PromptDiffEntry(BaseModel):
    base_variant: dict[str, Any]
    variant: dict[str, Any]
    agent_diffs: list[dict[str, Any]]


class RunVariantRequest(BaseModel):
    input_payload: dict[str, Any] = Field(default_factory=dict)


def _mint_variant_ids(variants: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Assign a stable persisted ``id`` to any variant missing one (FAR-332 3b).

    The frontend mints ids on Duplicate, but a variant created without an id
    (legacy payloads) must still persist one so every variant is comparable.
    Returns a NEW list of dicts; the input is never mutated.
    """
    out: list[dict[str, Any]] = []
    for variant in variants:
        entry = dict(variant)
        if entry.get("id") is None:
            entry["id"] = str(uuid.uuid4())
        out.append(entry)
    return out


def _collect_snapshot_ids(variants: list[Any]) -> list[uuid.UUID]:
    """Collect the ``snapshot_id`` of every variant that carries one.

    Variants that are not dicts or that lack a snapshot_id are skipped — they
    cannot be cross-org referenced and contribute nothing to the ownership set.
    """
    out: list[uuid.UUID] = []
    for v in variants:
        if not isinstance(v, dict):
            continue
        raw = v.get("snapshot_id")
        if raw is None:
            continue
        if isinstance(raw, uuid.UUID):
            out.append(raw)
        else:
            out.append(uuid.UUID(str(raw)))
    return out


async def _assert_variant_ownership(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    snapshot_ids: list[uuid.UUID],
) -> None:
    """Reject a cross-org pipeline/snapshot reference with a 403 (fail closed).

    Runs the same ownership check the batch path uses so a group referencing a
    pipeline or snapshot outside the org is rejected at the write source (and
    on the single-run path) instead of leaking existence via a generic error.
    """
    if not await validate_batch_ownership(
        session,
        org_id=org_id,
        pipeline_id=pipeline_id,
        snapshot_ids=snapshot_ids,
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Variant group references a pipeline or snapshot outside your organisation.",
        ) from None


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


@router.post("", response_model=VariantGroupResponse, status_code=status.HTTP_201_CREATED)
@handle_db_errors(_CODE_VARIANTS_CREATE_GROUP)
async def create_group(
    req: CreateVariantGroupRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("variant.create"),
) -> dict[str, Any]:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            await _assert_variant_ownership(
                session,
                org_id=principal.organisation_id,
                pipeline_id=req.pipeline_id,
                snapshot_ids=_collect_snapshot_ids([v.model_dump() for v in req.variants]),
            )
            group = await create_variant_group(
                session,
                org_id=principal.organisation_id,
                pipeline_id=req.pipeline_id,
                name=req.name,
                variants=_mint_variant_ids([v.model_dump() for v in req.variants]),
                description=req.description,
                selection_strategy=req.selection_strategy,
                max_concurrent_runs=req.max_concurrent_runs,
                degraded_evals=req.degraded_evals,
            )
    except IntegrityError:
        _log.exception(_CODE_VARIANTS_CREATE_GROUP)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Resource conflict. The referenced pipeline may not exist.",
        ) from None
    except ProgrammingError:
        _log.exception(_CODE_VARIANTS_CREATE_GROUP)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception(_CODE_VARIANTS_CREATE_GROUP)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_OCCURRED_PLEASE,
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception(_MSG_UNEXPECTED_ERROR_VARIANT_GROUP)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=_MSG_UNEXPECTED_ERROR_OCCURRED_PLEASE,
        ) from None

    return _variant_to_response(group)


@router.get("", response_model=list[VariantGroupResponse])
@handle_db_errors(_CODE_VARIANTS_LIST_GROUPS)
async def list_groups(
    pipeline_id: uuid.UUID | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_VARIANT_LIST),
) -> list[dict[str, Any]]:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            items, _total = await list_variant_groups(session, pipeline_id=pipeline_id, page=page, page_size=page_size)
    except IntegrityError:
        _log.exception(_CODE_VARIANTS_LIST_GROUPS)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MSG_RESOURCE_ALREADY_EXISTS,
        ) from None
    except ProgrammingError:
        _log.exception(_CODE_VARIANTS_LIST_GROUPS)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception(_CODE_VARIANTS_LIST_GROUPS)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_OCCURRED_PLEASE,
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception("Unexpected error in variant group list endpoint")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=_MSG_UNEXPECTED_ERROR_OCCURRED_PLEASE,
        ) from None
    return [_variant_to_response(g) for g in items]


@router.get("/{group_id}", response_model=VariantGroupResponse)
@handle_db_errors(_CODE_VARIANTS_GET_GROUP)
async def get_group(
    group_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_VARIANT_LIST),
) -> dict[str, Any]:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            group = await get_variant_group(session, group_id)
    except IntegrityError:
        _log.exception(_CODE_VARIANTS_GET_GROUP)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MSG_RESOURCE_ALREADY_EXISTS,
        ) from None
    except ProgrammingError:
        _log.exception(_CODE_VARIANTS_GET_GROUP)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception(_CODE_VARIANTS_GET_GROUP)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_OCCURRED_PLEASE,
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception(_MSG_UNEXPECTED_ERROR_VARIANT_GROUP)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=_MSG_UNEXPECTED_ERROR_OCCURRED_PLEASE,
        ) from None
    if group is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_VARIANT_GROUP_NOT_FOUND)
    return _variant_to_response(group)


@router.put("/{group_id}", response_model=VariantGroupResponse)
@handle_db_errors(_CODE_VARIANTS_UPDATE_GROUP)
async def update_group(
    group_id: uuid.UUID,
    req: CreateVariantGroupRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("variant.update"),
) -> dict[str, Any]:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            await _assert_variant_ownership(
                session,
                org_id=principal.organisation_id,
                pipeline_id=req.pipeline_id,
                snapshot_ids=_collect_snapshot_ids([v.model_dump() for v in req.variants]),
            )
            group = await update_variant_group(
                session,
                group_id,
                name=req.name,
                description=req.description,
                variants=_mint_variant_ids([v.model_dump() for v in req.variants]),
                selection_strategy=req.selection_strategy,
                max_concurrent_runs=req.max_concurrent_runs,
                degraded_evals=req.degraded_evals,
            )
    except IntegrityError:
        _log.exception(_CODE_VARIANTS_UPDATE_GROUP)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Resource conflict. The referenced pipeline may not exist.",
        ) from None
    except ProgrammingError:
        _log.exception(_CODE_VARIANTS_UPDATE_GROUP)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception(_CODE_VARIANTS_UPDATE_GROUP)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_OCCURRED_PLEASE,
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception(_MSG_UNEXPECTED_ERROR_VARIANT_GROUP)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=_MSG_UNEXPECTED_ERROR_OCCURRED_PLEASE,
        ) from None
    if group is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_VARIANT_GROUP_NOT_FOUND)
    return _variant_to_response(group)


@router.delete("/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
@handle_db_errors(_CODE_VARIANTS_DELETE_GROUP)
async def delete_group(
    group_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("variant.delete"),
) -> None:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            deleted = await soft_delete_variant_group(session, group_id)
    except IntegrityError:
        _log.exception(_CODE_VARIANTS_DELETE_GROUP)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot delete variant group — it is referenced by existing runs.",
        ) from None
    except ProgrammingError:
        _log.exception(_CODE_VARIANTS_DELETE_GROUP)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception(_CODE_VARIANTS_DELETE_GROUP)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_OCCURRED_PLEASE,
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception("Unexpected error in variant group delete endpoint")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=_MSG_UNEXPECTED_ERROR_OCCURRED_PLEASE,
        ) from None
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_VARIANT_GROUP_NOT_FOUND)


@router.post("/{group_id}/restore", response_model=VariantGroupResponse)
@handle_db_errors(_CODE_VARIANTS_RESTORE_GROUP)
async def restore_group(
    group_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("variant.create"),
) -> dict[str, Any]:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            group = await restore_variant_group(session, group_id)
    except IntegrityError:
        _log.exception(_CODE_VARIANTS_RESTORE_GROUP)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Resource conflict.",
        ) from None
    except ProgrammingError:
        _log.exception(_CODE_VARIANTS_RESTORE_GROUP)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception(_CODE_VARIANTS_RESTORE_GROUP)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_OCCURRED_PLEASE,
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception("Unexpected error in variant group restore endpoint")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=_MSG_UNEXPECTED_ERROR_OCCURRED_PLEASE,
        ) from None
    if group is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Variant group not found or not deleted")
    return _variant_to_response(group)


@router.post("/{group_id}/run", response_model=RunVariantResponse)
@handle_db_errors(_CODE_VARIANTS_RUN_VARIANT)
async def run_variant(
    group_id: uuid.UUID,
    req: RunVariantRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("variant.run"),
) -> dict[str, Any]:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            group = await get_variant_group(session, group_id)
            if group is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=_MSG_VARIANT_GROUP_NOT_FOUND,
                )

            if not group.variants:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="Variant group has no variants configured",
                )

            # Server-side ownership validation (FAR-332 3f): fail closed with a
            # 403 when the group references a pipeline or snapshot outside the
            # org, mirroring the batch path.
            await _assert_variant_ownership(
                session,
                org_id=principal.organisation_id,
                pipeline_id=group.pipeline_id,
                snapshot_ids=_collect_snapshot_ids(group.variants),
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
        _log.exception(_CODE_VARIANTS_RUN_VARIANT)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Resource conflict. The referenced pipeline or snapshot may not exist.",
        ) from None
    except ProgrammingError:
        _log.exception(_CODE_VARIANTS_RUN_VARIANT)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception(_CODE_VARIANTS_RUN_VARIANT)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_OCCURRED_PLEASE,
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception("Unexpected error in variant group run endpoint")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=_MSG_UNEXPECTED_ERROR_OCCURRED_PLEASE,
        ) from None

    if result is None:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Pipeline concurrent run quota exceeded",
        ) from None

    return {
        "run_id": result["run_id"],
        "variant_name": result["variant"].get("name", "unknown"),
        "merged_payload": _mask_output_value(result["merged_payload"]),
    }


@router.post("/{group_id}/batch-run", response_model=RunVariantBatchResponse)
@handle_db_errors(_CODE_VARIANTS_RUN_VARIANT_BATCH)
async def run_batch(
    group_id: uuid.UUID,
    req: RunVariantRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("variant.run"),
) -> dict[str, Any]:
    has_evals = True
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            group = await get_variant_group(session, group_id)
            if group is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=_MSG_VARIANT_GROUP_NOT_FOUND,
                )

            if not group.variants:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="Variant group has no variants configured",
                )

            # Server-side ownership validation (FAR-332 3f): every variant's
            # snapshot must belong to the org, not just the group. Fail closed
            # with a 403 (never leak existence).
            await _assert_variant_ownership(
                session,
                org_id=principal.organisation_id,
                pipeline_id=group.pipeline_id,
                snapshot_ids=_collect_snapshot_ids(group.variants),
            )

            results = await run_variant_batch(
                session,
                org_id=principal.organisation_id,
                group=group,
                input_payload=req.input_payload,
                account_id=principal.account_id,
            )
            if results is not None and results:
                has_evals = await has_pipeline_default_evals(session, group.pipeline_id)
    except IntegrityError:
        _log.exception(_CODE_VARIANTS_RUN_VARIANT_BATCH)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Resource conflict. The referenced pipeline or snapshot may not exist.",
        ) from None
    except ProgrammingError:
        _log.exception(_CODE_VARIANTS_RUN_VARIANT_BATCH)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception(_CODE_VARIANTS_RUN_VARIANT_BATCH)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_OCCURRED_PLEASE,
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception("Unexpected error in variant group batch run endpoint")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=_MSG_UNEXPECTED_ERROR_OCCURRED_PLEASE,
        ) from None

    if results is None:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=("variant_group_quota_exceeded: firing all variants would breach the pipeline concurrent run quota"),
        ) from None

    runs = [
        {
            "run_id": r["run_id"],
            "variant_name": r["variant"].get("name", "unknown"),
            "merged_payload": _mask_output_value(r["merged_payload"]),
        }
        for r in results
    ]
    batch_id = results[0].get("batch_id") if results else None
    return {
        "runs": runs,
        "count": len(runs),
        "batch_id": batch_id,
        "has_evals": has_evals,
    }


@router.get("/{group_id}/coverage-gaps", response_model=list[CoverageGap])
@handle_db_errors(_CODE_VARIANTS_COVERAGE_GAPS)
async def coverage_gaps(
    group_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_VARIANT_LIST),
) -> list[dict[str, Any]]:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            group = await get_variant_group(session, group_id)
            if group is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=_MSG_VARIANT_GROUP_NOT_FOUND,
                )
            gaps = await get_coverage_gaps(session, group)
    except IntegrityError:
        _log.exception(_CODE_VARIANTS_COVERAGE_GAPS)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MSG_RESOURCE_ALREADY_EXISTS,
        ) from None
    except ProgrammingError:
        _log.exception(_CODE_VARIANTS_COVERAGE_GAPS)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception(_CODE_VARIANTS_COVERAGE_GAPS)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_OCCURRED_PLEASE,
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception("Unexpected error in variant group coverage-gaps endpoint")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=_MSG_UNEXPECTED_ERROR_OCCURRED_PLEASE,
        ) from None
    return gaps


@router.get("/{group_id}/prompt-diffs", response_model=list[PromptDiffEntry])
@handle_db_errors(_CODE_VARIANTS_PROMPT_DIFFS)
async def prompt_diffs(
    group_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_VARIANT_LIST),
) -> list[dict[str, Any]]:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            group = await get_variant_group(session, group_id)
            if group is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=_MSG_VARIANT_GROUP_NOT_FOUND,
                )
            diffs = await get_prompt_diffs(session, group)
    except IntegrityError:
        _log.exception(_CODE_VARIANTS_PROMPT_DIFFS)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MSG_RESOURCE_ALREADY_EXISTS,
        ) from None
    except ProgrammingError:
        _log.exception(_CODE_VARIANTS_PROMPT_DIFFS)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception(_CODE_VARIANTS_PROMPT_DIFFS)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_OCCURRED_PLEASE,
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception("Unexpected error in variant group prompt-diffs endpoint")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=_MSG_UNEXPECTED_ERROR_OCCURRED_PLEASE,
        ) from None
    return diffs


@router.get("/batches/{batch_id}/compare", response_model=BatchCompareResponse)
@handle_db_errors(_CODE_VARIANTS_BATCH_COMPARE)
async def batch_compare(
    batch_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_VARIANT_LIST),
) -> dict[str, Any]:
    """Batch-scoped variant comparison (FAR-332 3d).

    Loads a batch's runs purely by ``batch_id`` — never by a live variant group,
    so soft-deleting the group does not break comparison. Org-scoped (RLS +
    explicit organisation_id predicate) so another org's batch_id returns 404.
    Each run carries its canonical status, eval pass rate, cost, tokens, and the
    frozen snapshot/override diff captured at fire time.
    """
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            entries = await get_batch_compare(session, org_id=principal.organisation_id, batch_id=batch_id)
            has_evals = False
            if entries:
                # Eval-coverage signal is per-pipeline: derive from the batch's
                # first run's pipeline (all runs in one batch share it).
                first_run = await get_run(session, entries[0]["run_id"], organisation_id=principal.organisation_id)
                has_evals = first_run is not None and await has_pipeline_default_evals(session, first_run.pipeline_id)
    except IntegrityError:
        _log.exception(_CODE_VARIANTS_BATCH_COMPARE)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MSG_RESOURCE_ALREADY_EXISTS,
        ) from None
    except ProgrammingError:
        _log.exception(_CODE_VARIANTS_BATCH_COMPARE)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        _log.exception(_CODE_VARIANTS_BATCH_COMPARE)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_MSG_DATABASE_ERROR_OCCURRED_PLEASE,
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception("Unexpected error in variant group batch compare endpoint")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=_MSG_UNEXPECTED_ERROR_OCCURRED_PLEASE,
        ) from None

    if not entries:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Batch not found",
        )

    # Sensitive masking (FAR-332 3i): frozen run_context_overrides may hold
    # secrets; never surface them in plaintext on the compare surface. Use the
    # same RECURSIVE util as the runs response (merged_payload) so a secret
    # nested under a non-sensitive key is masked at any depth — a shallow
    # top-level-key match would leak it verbatim.
    for entry in entries:
        entry["run_context_overrides"] = _mask_output_value(entry.get("run_context_overrides", {}))
        diff = entry.get("override_diff", {})
        if isinstance(diff, dict):
            # ``removed`` carries the base run's frozen override values — which
            # may hold secrets — so it must be masked like added/changed.
            for part in ("added", "changed", "removed"):
                raw = diff.get(part, {})
                if isinstance(raw, dict):
                    diff[part] = _mask_output_value(raw)

    return {"batch_id": batch_id, "has_evals": has_evals, "runs": entries}
