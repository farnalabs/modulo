"""Admin-only routes for cross-tenant organisation management."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.dependencies import get_db_session
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.auth.passwords import hash_password, validate_password_strength
from modulo.db.crud.account import create_account, get_account_by_email
from modulo.db.crud.org_membership import create_membership
from modulo.db.crud.organisation import (
    create_organisation,
    delete_organisation,
    get_organisation,
    get_organisation_by_slug,
    list_organisations,
    update_organisation,
)

router = APIRouter(prefix="/api/v1/admin/orgs", tags=["admin"])


# ── Create Org ──────────────────────────────────────────────────────────


class CreateOrgRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    slug: str = Field(
        min_length=3,
        max_length=63,
        pattern=r"^[a-z0-9-]+$",
    )
    plan_id: str | None = None


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
    if not current_user.is_system_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="System admin role required",
        )

    async with session.begin():
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
            plan_id=body.plan_id,
            created_by=current_user.account_id,
        )

        return CreateOrgResponse(
            id=str(org.id),
            name=org.name,
            slug=org.slug,
            status=org.status,
            created_at=org.created_at.isoformat(),
        )


# ── List Orgs ──────────────────────────────────────────────────────────


class ListOrgItem(BaseModel):
    id: str
    name: str
    slug: str
    plan_id: str | None = None
    status: str
    created_at: str


@router.get("", response_model=list[ListOrgItem])
async def admin_list_orgs(
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> list[ListOrgItem]:
    if not current_user.is_system_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="System admin role required")

    async with session.begin():
        orgs = await list_organisations(session)
    return [
        ListOrgItem(
            id=str(o.id),
            name=o.name,
            slug=o.slug,
            plan_id=o.plan_id,
            status=o.status,
            created_at=o.created_at.isoformat(),
        )
        for o in orgs
    ]


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
    if not current_user.is_system_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="System admin role required",
        )

    if body.org_role not in ("admin", "operator", "runner", "viewer"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid role: {body.org_role}. Must be one of: admin, operator, runner, viewer",
        )

    try:
        validate_password_strength(body.password)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    async with session.begin():
        org = await get_organisation(session, org_id)
        if org is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Organisation not found",
            )

        existing = await get_account_by_email(session, body.email)
        if existing is not None:
            from modulo.db.crud.org_membership import get_membership_by_account_and_org

            membership = await get_membership_by_account_and_org(session, existing.id, org_id)
            if membership is not None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="A user with this email already exists in this organisation",
                )

        pw_hash = hash_password(body.password)

        if existing is not None:
            account = existing
            account.password_hash = pw_hash
        else:
            account = await create_account(
                session,
                email=body.email,
                display_name=body.display_name,
                password_hash=pw_hash,
            )

        membership = await create_membership(
            session,
            account_id=account.id,
            org_id=org_id,
            role=body.org_role,
        )

        return CreateOrgUserResponse(
            id=str(account.id),
            email=account.email,
            display_name=account.display_name,
            org_role=membership.role,
            auth_provider=account.auth_provider,
            created_at=account.created_at.isoformat(),
        )


# ── Delete Org ─────────────────────────────────────────────────────────


@router.delete("/{org_id}", status_code=status.HTTP_204_NO_CONTENT)
async def admin_delete_org(
    org_id: uuid.UUID,
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> None:
    if not current_user.is_system_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="System admin role required")

    async with session.begin():
        org = await get_organisation(session, org_id)
        if org is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organisation not found")

        deleted = await delete_organisation(session, org_id)
        if not deleted:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organisation not found")


# ── Org License Management ──────────────────────────────────────────────


class OrgLicenseResponse(BaseModel):
    has_license: bool
    tier: str = "community"
    features: list[str] = Field(default_factory=list)
    expires_at: str | None = None
    org_id: str | None = None


class SetOrgLicenseRequest(BaseModel):
    license_key: str = Field(min_length=1)


@router.get("/{org_id}/license", response_model=OrgLicenseResponse)
async def admin_get_org_license(
    org_id: uuid.UUID,
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> OrgLicenseResponse:
    if not current_user.is_system_admin and current_user.organisation_id != org_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")

    org = await get_organisation(session, org_id)
    if org is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organisation not found")

    from modulo.core.license import get_license as get_sys_license, parse_and_verify

    org_key = org.settings_json.get("license_key") if org.settings_json else None
    if org_key:
        validation = parse_and_verify(org_key)
        if validation.valid and validation.license_data is not None:
            d = validation.license_data
            return OrgLicenseResponse(
                has_license=True,
                tier=d.tier,
                features=d.features,
                expires_at=d.expires_at or None,
                org_id=d.org_id or None,
            )

    lic = get_sys_license()
    if lic is not None:
        return OrgLicenseResponse(
            has_license=True,
            tier=lic.tier,
            features=lic.features,
            expires_at=lic.expires_at or None,
            org_id=lic.org_id or None,
        )

    return OrgLicenseResponse(has_license=False)


@router.put("/{org_id}/license", response_model=OrgLicenseResponse)
async def admin_set_org_license(
    org_id: uuid.UUID,
    body: SetOrgLicenseRequest,
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> OrgLicenseResponse:
    if not current_user.is_system_admin and current_user.organisation_id != org_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")

    org = await get_organisation(session, org_id)
    if org is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organisation not found")

    from modulo.core.license import parse_and_verify

    try:
        validation = parse_and_verify(body.license_key)
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc

    if not validation.valid or validation.license_data is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=validation.error or "Invalid license key",
        )

    settings_json = dict(org.settings_json or {})
    settings_json["license_key"] = body.license_key
    await update_organisation(session, org_id, {"settings_json": settings_json})

    d = validation.license_data
    return OrgLicenseResponse(
        has_license=True,
        tier=d.tier,
        features=d.features,
        expires_at=d.expires_at or None,
        org_id=d.org_id or None,
    )


@router.delete("/{org_id}/license", response_model=OrgLicenseResponse)
async def admin_remove_org_license(
    org_id: uuid.UUID,
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> OrgLicenseResponse:
    if not current_user.is_system_admin and current_user.organisation_id != org_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")

    org = await get_organisation(session, org_id)
    if org is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organisation not found")

    settings_json = dict(org.settings_json or {})
    had_key = "license_key" in settings_json
    settings_json.pop("license_key", None)
    await update_organisation(session, org_id, {"settings_json": settings_json})

    return OrgLicenseResponse(has_license=False)
