"""Admin-only routes for cross-tenant organisation management."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.dependencies import get_db_session
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.auth.passwords import hash_password, validate_password_strength
from modulo.db.crud.organisation import (
    create_organisation,
    get_organisation,
    get_organisation_by_slug,
)
from modulo.db.crud.user import create_user, get_user_by_email
from modulo.db.models.user import User

router = APIRouter(prefix="/api/v1/admin/orgs", tags=["admin"])


# ── Create Org ──────────────────────────────────────────────────────────


class CreateOrgRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    slug: str = Field(
        min_length=3,
        max_length=63,
        pattern=r"^[a-z0-9-]+$",
    )


class CreateOrgResponse(BaseModel):
    id: str
    name: str
    slug: str
    status: str
    created_at: str


@router.post("", response_model=CreateOrgResponse, status_code=status.HTTP_201_CREATED)
async def admin_create_org(
    body: CreateOrgRequest,
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> CreateOrgResponse:
    if current_user.org_role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admin users can create organisations",
        )

    existing = await get_organisation_by_slug(session, body.slug)
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"An organisation with slug '{body.slug}' already exists",
        )

    org = await create_organisation(
        session,
        name=body.name,
        slug=body.slug,
        created_by=current_user.user_id,
    )

    return CreateOrgResponse(
        id=str(org.id),
        name=org.name,
        slug=org.slug,
        status=org.status,
        created_at=org.created_at.isoformat(),
    )


# ── Create User in Org ─────────────────────────────────────────────────


class CreateOrgUserRequest(BaseModel):
    email: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    password: str = Field(min_length=8)
    org_role: str = Field(default="runner")


class CreateOrgUserResponse(BaseModel):
    id: str
    email: str
    display_name: str
    org_role: str
    auth_provider: str
    created_at: str


@router.post("/{org_id}/users", response_model=CreateOrgUserResponse, status_code=status.HTTP_201_CREATED)
async def admin_create_org_user(
    org_id: uuid.UUID,
    body: CreateOrgUserRequest,
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> CreateOrgUserResponse:
    if current_user.org_role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admin users can create users in organisations",
        )

    if body.org_role not in ("admin", "operator", "runner", "viewer"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid role: {body.org_role}. Must be one of: admin, operator, runner, viewer",
        )

    org = await get_organisation(session, org_id)
    if org is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Organisation not found",
        )

    existing = await get_user_by_email(session, body.email)
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A user with this email already exists",
        )

    try:
        validate_password_strength(body.password)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    pw_hash = hash_password(body.password)
    user = await create_user(
        session,
        org_id=org_id,
        email=body.email,
        display_name=body.display_name,
        password_hash=pw_hash,
        org_role=body.org_role,
    )

    return CreateOrgUserResponse(
        id=str(user.id),
        email=user.email,
        display_name=user.display_name,
        org_role=user.org_role,
        auth_provider=user.auth_provider,
        created_at=user.created_at.isoformat(),
    )
