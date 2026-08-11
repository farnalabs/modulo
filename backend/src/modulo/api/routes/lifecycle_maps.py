"""Lifecycle Map CRUD + version REST API."""

import logging
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.db_error_handling import handle_db_errors
from modulo.api.dependencies import get_db_session, require_permission
from modulo.auth.jwt import TenantPrincipal
from modulo.core.lifecycle_map.service import (
    create_lifecycle_map,
    delete_lifecycle_map,
    get_lifecycle_map,
    graduate_stage,
    list_lifecycle_maps,
    restore_lifecycle_map,
    save_map_version,
    update_lifecycle_map,
)
from modulo.core.lifecycle_map.validation import (
    LifecycleMapContentError,
    LifecycleMapPipelineConflictError,
)
from modulo.db.rls import set_rls_org, set_rls_user_context

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/lifecycle-maps", tags=["lifecycle_maps"])


class LifecycleMapCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(None, max_length=2000)
    owner_team_id: uuid.UUID | None = None
    visibility: str = Field(default="org", pattern=r"^(org|team)$")
    version: int = Field(default=1, ge=1)
    content_json: dict[str, Any] = Field(default_factory=dict[str, Any])

    @model_validator(mode="after")
    def _validate_team_visibility(self) -> "LifecycleMapCreate":
        if self.visibility == "team" and self.owner_team_id is None:
            raise ValueError("owner_team_id is required when visibility is 'team'")
        return self


class LifecycleMapUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=255)
    description: str | None = Field(None, max_length=2000)
    owner_team_id: uuid.UUID | None = None
    visibility: str | None = Field(None, pattern=r"^(org|team)$")
    content_json: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _validate_team_visibility(self) -> "LifecycleMapUpdate":
        if self.visibility == "team" and self.owner_team_id is None:
            raise ValueError("owner_team_id is required when visibility is 'team'")
        return self


class LifecycleMapResponse(BaseModel):
    id: uuid.UUID
    organisation_id: uuid.UUID
    name: str
    description: str | None
    owner_team_id: uuid.UUID | None
    visibility: str
    version: int
    content_json: dict[str, Any]
    archived_at: datetime | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True, "populate_by_name": True}


class LifecycleMapListResponse(BaseModel):
    items: list[LifecycleMapResponse]
    total: int
    page: int
    page_size: int


class VersionSaveRequest(BaseModel):
    """Stage/edge canvas payload POSTed by the visual editor.

    Stages/edges are opaque dicts so editor fields survive round-trips; the
    shape is validated and canonicalised by ``normalize_content``.
    """

    stages: list[dict[str, Any]] = Field(default_factory=list)
    edges: list[dict[str, Any]] = Field(default_factory=list)
    notes: str = Field(default="", max_length=4000)


class GraduateStageRequest(BaseModel):
    pipeline_id: str | None = None


class LifecycleMapStageEditorItem(BaseModel):
    """A journey/map-stage in the editor wire shape."""

    id: str
    name: str
    description: str | None = None
    stage_type: str
    pipeline_id: str | None = None
    external_url: str | None = None
    owner: str | None = None
    graduated: bool = False


class LifecycleMapEdgeEditorItem(BaseModel):
    """A transition edge in the editor wire shape."""

    id: str
    source_stage_id: str
    target_stage_id: str
    trigger_type: str | None = None
    description: str | None = None
    condition_expression: str | None = None
    estimated_frequency: str | None = None
    trigger_link: str | None = None


class LifecycleMapVersionResponse(BaseModel):
    id: uuid.UUID
    lifecycle_map_id: uuid.UUID
    version: int
    version_number: int
    stages: list[LifecycleMapStageEditorItem]
    edges: list[LifecycleMapEdgeEditorItem]
    created_by: str | None = None
    created_at: datetime
    notes: str = ""


class LifecycleMapStageItem(BaseModel):
    """A journey/map-stage in the map-detail wire shape (store/read path)."""

    id: str
    name: str
    description: str | None = None
    type: str
    owner_badge: str | None = None
    graduated: bool = False
    pipeline_id: str | None = None
    external_url: str | None = None


class LifecycleMapTransitionItem(BaseModel):
    id: str
    source_stage_id: str
    target_stage_id: str
    trigger_type: str | None = None
    description: str | None = None


class LifecycleMapVersionMeta(BaseModel):
    version: int
    created_at: datetime
    created_by: str | None = None


class LifecycleMapDetailResponse(BaseModel):
    id: uuid.UUID
    organisation_id: uuid.UUID
    name: str
    description: str | None
    owner: str | None = None
    owner_team_id: uuid.UUID | None
    visibility: str
    version: int
    current_version: int
    stages: list[LifecycleMapStageItem]
    transitions: list[LifecycleMapTransitionItem]
    versions: list[LifecycleMapVersionMeta]
    content_json: dict[str, Any]
    archived_at: datetime | None
    created_at: datetime
    updated_at: datetime


def _content_dict(lm: Any) -> dict[str, Any]:
    content = getattr(lm, "content_json", None)
    return content if isinstance(content, dict) else {}


def _build_version_entry(lm: Any) -> LifecycleMapVersionResponse:
    """Serialize the active map state as a version entry.

    v1 has no immutable version history: the map's current content_json is
    returned as the single active version, keyed by the map id so the editor
    can round-trip through PUT.
    """
    content = _content_dict(lm)
    stages = [
        LifecycleMapStageEditorItem(
            id=s.get("id", ""),
            name=s.get("name", ""),
            description=s.get("description"),
            stage_type=s.get("type", "placeholder"),
            pipeline_id=s.get("pipeline_id"),
            external_url=s.get("external_url"),
            owner=s.get("owner"),
            graduated=bool(s.get("graduated", False)),
        )
        for s in (content.get("stages") or [])
        if isinstance(s, dict)
    ]
    edges = [
        LifecycleMapEdgeEditorItem(
            id=e.get("id", ""),
            source_stage_id=e.get("source", ""),
            target_stage_id=e.get("target", ""),
            trigger_type=e.get("trigger_type"),
            description=e.get("description"),
            condition_expression=e.get("condition_expression"),
            estimated_frequency=e.get("estimated_frequency"),
            trigger_link=e.get("trigger_link"),
        )
        for e in (content.get("edges") or [])
        if isinstance(e, dict)
    ]
    notes = content.get("notes")
    return LifecycleMapVersionResponse(
        id=lm.id,
        lifecycle_map_id=lm.id,
        version=lm.version,
        version_number=lm.version,
        stages=stages,
        edges=edges,
        created_by=None,
        created_at=lm.updated_at,
        notes=notes if isinstance(notes, str) else "",
    )


def _build_detail(lm: Any) -> LifecycleMapDetailResponse:
    """Serialize the map in the store/read shape (decoded stages + version meta)."""
    content = _content_dict(lm)
    stages = [
        LifecycleMapStageItem(
            id=s.get("id", ""),
            name=s.get("name", ""),
            description=s.get("description"),
            type=s.get("type", "placeholder"),
            owner_badge=s.get("owner"),
            graduated=bool(s.get("graduated", False)),
            pipeline_id=s.get("pipeline_id"),
            external_url=s.get("external_url"),
        )
        for s in (content.get("stages") or [])
        if isinstance(s, dict)
    ]
    transitions = [
        LifecycleMapTransitionItem(
            id=e.get("id", ""),
            source_stage_id=e.get("source", ""),
            target_stage_id=e.get("target", ""),
            trigger_type=e.get("trigger_type"),
            description=e.get("description"),
        )
        for e in (content.get("edges") or [])
        if isinstance(e, dict)
    ]
    return LifecycleMapDetailResponse(
        id=lm.id,
        organisation_id=lm.organisation_id,
        name=lm.name,
        description=lm.description,
        owner=None,
        owner_team_id=lm.owner_team_id,
        visibility=lm.visibility,
        version=lm.version,
        current_version=lm.version,
        stages=stages,
        transitions=transitions,
        versions=[LifecycleMapVersionMeta(version=lm.version, created_at=lm.updated_at, created_by=None)],
        content_json=content,
        archived_at=lm.archived_at,
        created_at=lm.created_at,
        updated_at=lm.updated_at,
    )


@handle_db_errors("lifecycle_maps.list_lifecycle_maps_endpoint")
@router.get("", response_model=LifecycleMapListResponse)
async def list_lifecycle_maps_endpoint(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    owner_team_id: uuid.UUID | None = Query(default=None),
    include_archived: bool = Query(default=False),
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("lifecycle_map.list"),
) -> LifecycleMapListResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            result = await list_lifecycle_maps(
                session,
                page=page,
                page_size=page_size,
                owner_team_id=owner_team_id,
                include_archived=include_archived,
            )
    except ProgrammingError as exc:
        _log.exception("lifecycle_maps.list_lifecycle_maps_endpoint")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from exc
    except SQLAlchemyError as exc:
        _log.exception("lifecycle_maps.list_lifecycle_maps_endpoint")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database temporarily unavailable.",
        ) from exc
    except HTTPException:
        raise
    except Exception:
        _log.exception("lifecycle_maps.list")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred.",
        ) from None
    return LifecycleMapListResponse(
        items=[LifecycleMapResponse.model_validate(m) for m in result.items],
        total=result.total,
        page=result.page,
        page_size=result.page_size,
    )


@handle_db_errors("lifecycle_maps.create_lifecycle_map_endpoint")
@router.post("", response_model=LifecycleMapResponse, status_code=status.HTTP_201_CREATED)
async def create_lifecycle_map_endpoint(
    req: LifecycleMapCreate,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("lifecycle_map.create"),
) -> LifecycleMapResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            lifecycle_map = await create_lifecycle_map(
                session,
                org_id=principal.organisation_id,
                name=req.name,
                account_id=principal.account_id,
                description=req.description,
                owner_team_id=req.owner_team_id,
                visibility=req.visibility,
                version=req.version,
                content_json=req.content_json,
            )
    except LifecycleMapPipelineConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    except LifecycleMapContentError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from None
    except ProgrammingError as exc:
        _log.exception("lifecycle_maps.create_lifecycle_map_endpoint")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from exc
    except IntegrityError as exc:
        _log.exception("lifecycle_maps.create_lifecycle_map_endpoint")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Lifecycle map conflicts with an existing resource.",
        ) from exc
    except SQLAlchemyError as exc:
        _log.exception("lifecycle_maps.create_lifecycle_map_endpoint")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database temporarily unavailable.",
        ) from exc
    except HTTPException:
        raise
    except Exception:
        _log.exception("lifecycle_maps.create")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred.",
        ) from None
    return LifecycleMapResponse.model_validate(lifecycle_map)


@handle_db_errors("lifecycle_maps.get_lifecycle_map_endpoint")
@router.get("/{lifecycle_map_id}", response_model=LifecycleMapDetailResponse)
async def get_lifecycle_map_endpoint(
    lifecycle_map_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("lifecycle_map.list"),
) -> LifecycleMapDetailResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            lifecycle_map = await get_lifecycle_map(session, lifecycle_map_id)
    except ProgrammingError as exc:
        _log.exception("lifecycle_maps.get_lifecycle_map_endpoint")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from exc
    except SQLAlchemyError as exc:
        _log.exception("lifecycle_maps.get_lifecycle_map_endpoint")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database temporarily unavailable.",
        ) from exc
    except HTTPException:
        raise
    except Exception:
        _log.exception("lifecycle_maps.get")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred.",
        ) from None
    if lifecycle_map is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lifecycle map not found")
    return _build_detail(lifecycle_map)


@handle_db_errors("lifecycle_maps.update_lifecycle_map_endpoint")
@router.put("/{lifecycle_map_id}", response_model=LifecycleMapResponse)
async def update_lifecycle_map_endpoint(
    lifecycle_map_id: uuid.UUID,
    req: LifecycleMapUpdate,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("lifecycle_map.update"),
) -> LifecycleMapResponse:
    updates = req.model_dump(exclude_unset=True)
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            current = await get_lifecycle_map(session, lifecycle_map_id)
            if current is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lifecycle map not found")
            if "content_json" in updates:
                updates["version"] = current.version + 1
            lifecycle_map = await update_lifecycle_map(session, lifecycle_map_id, updates)
    except LifecycleMapPipelineConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    except LifecycleMapContentError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from None
    except ProgrammingError as exc:
        _log.exception("lifecycle_maps.update_lifecycle_map_endpoint")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from exc
    except IntegrityError as exc:
        _log.exception("lifecycle_maps.update_lifecycle_map_endpoint")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Lifecycle map conflicts with an existing resource.",
        ) from exc
    except SQLAlchemyError as exc:
        _log.exception("lifecycle_maps.update_lifecycle_map_endpoint")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database temporarily unavailable.",
        ) from exc
    except HTTPException:
        raise
    except Exception:
        _log.exception("lifecycle_maps.update")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred.",
        ) from None
    return LifecycleMapResponse.model_validate(lifecycle_map)


@handle_db_errors("lifecycle_maps.delete_lifecycle_map_endpoint")
@router.delete("/{lifecycle_map_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_lifecycle_map_endpoint(
    lifecycle_map_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("lifecycle_map.delete"),
) -> None:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            deleted = await delete_lifecycle_map(session, lifecycle_map_id)
    except ProgrammingError as exc:
        _log.exception("lifecycle_maps.delete_lifecycle_map_endpoint")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from exc
    except SQLAlchemyError as exc:
        _log.exception("lifecycle_maps.delete_lifecycle_map_endpoint")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database temporarily unavailable.",
        ) from exc
    except HTTPException:
        raise
    except Exception:
        _log.exception("lifecycle_maps.delete")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred.",
        ) from None
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lifecycle map not found")


@handle_db_errors("lifecycle_maps.restore_lifecycle_map_endpoint")
@router.post("/{lifecycle_map_id}/restore", response_model=LifecycleMapResponse)
async def restore_lifecycle_map_endpoint(
    lifecycle_map_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("lifecycle_map.create"),
) -> LifecycleMapResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            lifecycle_map = await restore_lifecycle_map(session, lifecycle_map_id)
    except ProgrammingError as exc:
        _log.exception("lifecycle_maps.restore_lifecycle_map_endpoint")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from exc
    except IntegrityError as exc:
        _log.exception("lifecycle_maps.restore_lifecycle_map_endpoint")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Lifecycle map cannot be restored: a stage pipeline is already registered in another active map.",
        ) from exc
    except SQLAlchemyError as exc:
        _log.exception("lifecycle_maps.restore_lifecycle_map_endpoint")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database temporarily unavailable.",
        ) from exc
    except HTTPException:
        raise
    except Exception:
        _log.exception("lifecycle_maps.restore")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred.",
        ) from None
    if lifecycle_map is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lifecycle map not found or not deleted")
    return LifecycleMapResponse.model_validate(lifecycle_map)


@handle_db_errors("lifecycle_maps.list_versions_endpoint")
@router.get("/{lifecycle_map_id}/versions", response_model=list[LifecycleMapVersionResponse])
async def list_lifecycle_map_versions_endpoint(
    lifecycle_map_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("lifecycle_map.list"),
) -> list[LifecycleMapVersionResponse]:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            lifecycle_map = await get_lifecycle_map(session, lifecycle_map_id)
    except ProgrammingError as exc:
        _log.exception("lifecycle_maps.list_versions_endpoint")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from exc
    except SQLAlchemyError as exc:
        _log.exception("lifecycle_maps.list_versions_endpoint")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database temporarily unavailable.",
        ) from exc
    except HTTPException:
        raise
    except Exception:
        _log.exception("lifecycle_maps.list_versions")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred.",
        ) from None
    if lifecycle_map is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lifecycle map not found")
    return [_build_version_entry(lifecycle_map)]


@handle_db_errors("lifecycle_maps.save_version_endpoint")
@router.post(
    "/{lifecycle_map_id}/versions",
    response_model=LifecycleMapVersionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def save_lifecycle_map_version_endpoint(
    lifecycle_map_id: uuid.UUID,
    req: VersionSaveRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("lifecycle_map.update"),
) -> LifecycleMapVersionResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            lifecycle_map = await save_map_version(
                session,
                lifecycle_map_id,
                stages=req.stages,
                edges=req.edges,
                notes=req.notes,
            )
    except LifecycleMapPipelineConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    except LifecycleMapContentError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from None
    except ProgrammingError as exc:
        _log.exception("lifecycle_maps.save_version_endpoint")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from exc
    except IntegrityError as exc:
        _log.exception("lifecycle_maps.save_version_endpoint")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Map version conflicts with an existing lifecycle map resource.",
        ) from exc
    except SQLAlchemyError as exc:
        _log.exception("lifecycle_maps.save_version_endpoint")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database temporarily unavailable.",
        ) from exc
    except HTTPException:
        raise
    except Exception:
        _log.exception("lifecycle_maps.save_version")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred.",
        ) from None
    if lifecycle_map is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lifecycle map not found")
    return _build_version_entry(lifecycle_map)


@handle_db_errors("lifecycle_maps.update_version_endpoint")
@router.put("/{lifecycle_map_id}/versions/{version_id}", response_model=LifecycleMapVersionResponse)
async def update_lifecycle_map_version_endpoint(
    lifecycle_map_id: uuid.UUID,
    version_id: uuid.UUID,
    req: VersionSaveRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("lifecycle_map.update"),
) -> LifecycleMapVersionResponse:
    """Update a version. v1 semantics: the active map state is the only version,
    so this behaves identically to save — ``version_id`` is validated as a UUID
    for contract compatibility but the save targets the map itself.
    """
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            lifecycle_map = await save_map_version(
                session,
                lifecycle_map_id,
                stages=req.stages,
                edges=req.edges,
                notes=req.notes,
            )
    except LifecycleMapPipelineConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    except LifecycleMapContentError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from None
    except ProgrammingError as exc:
        _log.exception("lifecycle_maps.update_version_endpoint")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from exc
    except IntegrityError as exc:
        _log.exception("lifecycle_maps.update_version_endpoint")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Map version conflicts with an existing lifecycle map resource.",
        ) from exc
    except SQLAlchemyError as exc:
        _log.exception("lifecycle_maps.update_version_endpoint")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database temporarily unavailable.",
        ) from exc
    except HTTPException:
        raise
    except Exception:
        _log.exception("lifecycle_maps.update_version")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred.",
        ) from None
    if lifecycle_map is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lifecycle map not found")
    return _build_version_entry(lifecycle_map)


@handle_db_errors("lifecycle_maps.get_version_endpoint")
@router.get("/{lifecycle_map_id}/versions/{version}", response_model=LifecycleMapDetailResponse)
async def get_lifecycle_map_version_endpoint(
    lifecycle_map_id: uuid.UUID,
    version: int = Path(ge=1),
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("lifecycle_map.list"),
) -> LifecycleMapDetailResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            lifecycle_map = await get_lifecycle_map(session, lifecycle_map_id)
    except ProgrammingError as exc:
        _log.exception("lifecycle_maps.get_version_endpoint")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from exc
    except SQLAlchemyError as exc:
        _log.exception("lifecycle_maps.get_version_endpoint")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database temporarily unavailable.",
        ) from exc
    except HTTPException:
        raise
    except Exception:
        _log.exception("lifecycle_maps.get_version")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred.",
        ) from None
    if lifecycle_map is None or lifecycle_map.version != version:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lifecycle map version not found")
    return _build_detail(lifecycle_map)


@handle_db_errors("lifecycle_maps.graduate_stage_endpoint")
@router.patch(
    "/{lifecycle_map_id}/versions/{version_id}/stages/{stage_id}/graduate",
    response_model=LifecycleMapVersionResponse,
)
async def graduate_lifecycle_map_stage_endpoint(
    lifecycle_map_id: uuid.UUID,
    version_id: uuid.UUID,
    stage_id: str,
    req: GraduateStageRequest,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission("lifecycle_map.update"),
) -> LifecycleMapVersionResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            lifecycle_map = await graduate_stage(
                session,
                lifecycle_map_id,
                stage_id=stage_id,
                pipeline_id=req.pipeline_id,
            )
    except LifecycleMapPipelineConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    except LifecycleMapContentError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from None
    except ProgrammingError as exc:
        _log.exception("lifecycle_maps.graduate_stage_endpoint")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from exc
    except IntegrityError as exc:
        _log.exception("lifecycle_maps.graduate_stage_endpoint")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Map version conflicts with an existing lifecycle map resource.",
        ) from exc
    except SQLAlchemyError as exc:
        _log.exception("lifecycle_maps.graduate_stage_endpoint")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database temporarily unavailable.",
        ) from exc
    except HTTPException:
        raise
    except Exception:
        _log.exception("lifecycle_maps.graduate_stage")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred.",
        ) from None
    if lifecycle_map is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lifecycle map not found")
    return _build_version_entry(lifecycle_map)
