"""Lifecycle Map CRUD REST API."""

import logging
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.db_error_handling import handle_db_errors
from modulo.api.dependencies import get_db_session
from modulo.auth.dependencies import get_current_tenant_user
from modulo.auth.jwt import TenantPrincipal
from modulo.core.lifecycle_map.service import (
    create_lifecycle_map,
    delete_lifecycle_map,
    get_lifecycle_map,
    list_lifecycle_maps,
    update_lifecycle_map,
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


@handle_db_errors("lifecycle_maps.list_lifecycle_maps_endpoint")
@router.get("", response_model=LifecycleMapListResponse)
async def list_lifecycle_maps_endpoint(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    owner_team_id: uuid.UUID | None = Query(default=None),
    include_archived: bool = Query(default=False),
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
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
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from exc
    except SQLAlchemyError as exc:
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
    principal: TenantPrincipal = Depends(get_current_tenant_user),
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
    except ProgrammingError as exc:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from exc
    except SQLAlchemyError as exc:
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
@router.get("/{lifecycle_map_id}", response_model=LifecycleMapResponse)
async def get_lifecycle_map_endpoint(
    lifecycle_map_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> LifecycleMapResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            lifecycle_map = await get_lifecycle_map(session, lifecycle_map_id)
    except ProgrammingError as exc:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from exc
    except SQLAlchemyError as exc:
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
    return LifecycleMapResponse.model_validate(lifecycle_map)


@handle_db_errors("lifecycle_maps.update_lifecycle_map_endpoint")
@router.put("/{lifecycle_map_id}", response_model=LifecycleMapResponse)
async def update_lifecycle_map_endpoint(
    lifecycle_map_id: uuid.UUID,
    req: LifecycleMapUpdate,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
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
    except ProgrammingError as exc:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from exc
    except SQLAlchemyError as exc:
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
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> None:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            deleted = await delete_lifecycle_map(session, lifecycle_map_id)
    except ProgrammingError as exc:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from exc
    except SQLAlchemyError as exc:
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
