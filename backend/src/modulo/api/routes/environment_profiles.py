"""EnvironmentProfile CRUD REST API (v1)."""

import logging
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.db_error_handling import handle_db_errors
from modulo.api.dependencies import get_db_session
from modulo.auth.dependencies import get_current_tenant_user
from modulo.auth.jwt import TenantPrincipal
from modulo.db.crud.environment_profile import (
    create_environment_profile,
    delete_environment_profile,
    get_environment_profile,
    list_environment_profiles,
    update_environment_profile,
)
from modulo.db.models.environment_profile import EnvironmentProfile
from modulo.db.rls import set_rls_org, set_rls_user_context

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/environment-profiles", tags=["environment-profiles"])


class ProfileCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: str | None = Field(None, max_length=2000)
    provider_type: str = Field(default="local_docker")
    image_ref: str | None = Field(None, min_length=1, max_length=500)
    capabilities: list[str] = Field(default_factory=list)
    config_json: dict[str, Any] = Field(default_factory=dict)
    network_policy: str = Field(default="outbound")
    initialisation_strategy: str = Field(default="git_clone")
    secret_refs: list[str] = Field(default_factory=list)
    persistence_policy: str = Field(default="ephemeral")
    owner_team_id: uuid.UUID | None = None
    visibility: str = Field(default="org")


class ProfileUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=255)
    description: str | None = Field(None, max_length=2000)
    provider_type: str | None = None
    image_ref: str | None = Field(None, min_length=1, max_length=500)
    capabilities: list[str] | None = None
    config_json: dict[str, Any] | None = None
    network_policy: str | None = None
    initialisation_strategy: str | None = None
    secret_refs: list[str] | None = None
    persistence_policy: str | None = None
    owner_team_id: uuid.UUID | None = None
    visibility: str | None = None


class ProfileResponse(BaseModel):
    id: uuid.UUID
    organisation_id: uuid.UUID
    name: str
    description: str | None = None
    provider_type: str
    image_ref: str | None = None
    capabilities: list[str]
    config_json: dict[str, Any]
    network_policy: str
    initialisation_strategy: str
    secret_refs: list[str]
    persistence_policy: str
    status: str
    owner_team_id: uuid.UUID | None = None
    visibility: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True, "populate_by_name": True}


class ProfileListResponse(BaseModel):
    items: list[ProfileResponse]
    total: int
    page: int
    page_size: int


def _to_response(p: EnvironmentProfile) -> ProfileResponse:
    return ProfileResponse(
        id=p.id,
        organisation_id=p.organisation_id,
        name=p.name,
        description=p.description,
        provider_type=p.provider_type,
        image_ref=p.image_ref,
        capabilities=p.capabilities_json,
        config_json=p.config_json,
        network_policy=p.network_policy,
        initialisation_strategy=p.initialisation_strategy,
        secret_refs=p.secret_refs_json,
        persistence_policy=p.persistence_policy,
        status=p.status,
        owner_team_id=p.owner_team_id,
        visibility=p.visibility,
        created_at=p.created_at,
        updated_at=p.updated_at,
    )


@handle_db_errors("environment_profiles.list_profiles")
@router.get("", response_model=ProfileListResponse)
async def list_profiles(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> ProfileListResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            result = await list_environment_profiles(session, page=page, page_size=page_size)
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    except SQLAlchemyError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database error occurred. Please try again later.",
        ) from None
    except HTTPException:
        raise
    except Exception as exc:
        _log.exception("Unexpected error listing environment profiles: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred.",
        ) from None
    return ProfileListResponse(
        items=[_to_response(p) for p in result.items],
        total=result.total,
        page=result.page,
        page_size=result.page_size,
    )


@handle_db_errors("environment_profiles.create_profile")
@router.post("", response_model=ProfileResponse, status_code=status.HTTP_201_CREATED)
async def create_profile(
    req: ProfileCreate,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> ProfileResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            profile = await create_environment_profile(
                session,
                org_id=principal.organisation_id,
                name=req.name,
                account_id=principal.account_id,
                description=req.description,
                provider_type=req.provider_type,
                image_ref=req.image_ref,
                capabilities=req.capabilities,
                config_json=req.config_json,
                network_policy=req.network_policy,
                initialisation_strategy=req.initialisation_strategy,
                secret_refs=req.secret_refs,
                persistence_policy=req.persistence_policy,
                owner_team_id=req.owner_team_id,
                visibility=req.visibility,
            )
    except IntegrityError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An environment profile with this name already exists.",
        ) from None
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    except SQLAlchemyError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database error occurred. Please try again later.",
        ) from None
    except HTTPException:
        raise
    except Exception as exc:
        _log.exception("Unexpected error creating environment profile: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred.",
        ) from None
    return _to_response(profile)


@handle_db_errors("environment_profiles.get_profile")
@router.get("/{profile_id}", response_model=ProfileResponse)
async def get_profile(
    profile_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> ProfileResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            profile = await get_environment_profile(session, profile_id)
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    except SQLAlchemyError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database error occurred. Please try again later.",
        ) from None
    except Exception as exc:
        _log.exception("Unexpected error fetching environment profile: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred.",
        ) from None
    if profile is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Environment profile not found")
    return _to_response(profile)


@handle_db_errors("environment_profiles.update_profile")
@router.put("/{profile_id}", response_model=ProfileResponse)
async def update_profile(
    profile_id: uuid.UUID,
    req: ProfileUpdate,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> ProfileResponse:
    updates = req.model_dump(exclude_unset=True)
    if "capabilities" in updates:
        updates["capabilities_json"] = updates.pop("capabilities")
    if "secret_refs" in updates:
        updates["secret_refs_json"] = updates.pop("secret_refs")
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            profile = await update_environment_profile(session, profile_id, updates)
    except IntegrityError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An environment profile with this name already exists.",
        ) from None
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    except SQLAlchemyError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database error occurred. Please try again later.",
        ) from None
    except HTTPException:
        raise
    except Exception as exc:
        _log.exception("Unexpected error updating environment profile: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred.",
        ) from None
    if profile is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Environment profile not found")
    return _to_response(profile)


@handle_db_errors("environment_profiles.delete_profile")
@router.delete("/{profile_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_profile(
    profile_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = Depends(get_current_tenant_user),
) -> None:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            await set_rls_user_context(session, principal.account_id, principal.org_role)
            deleted = await delete_environment_profile(session, profile_id)
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    except SQLAlchemyError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database error occurred. Please try again later.",
        ) from None
    except Exception as exc:
        _log.exception("Unexpected error deleting environment profile: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred.",
        ) from None
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Environment profile not found")
