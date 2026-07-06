"""Admin license endpoint — view and update the deployment license key."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.dependencies import get_db_session
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.core.license import (
    LicenseError,
    get_license,
    parse_and_verify,
    store_license,
)
from modulo.db.crud.organisation import get_organisation
from modulo.settings import Settings, get_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/admin/license", tags=["admin-license"])


class LicenseStatusResponse(BaseModel):
    has_license: bool
    tier: str = "community"
    features: list[str] = Field(default_factory=list)
    expires_at: str | None = None
    org_id: str | None = None


class LicenseUploadRequest(BaseModel):
    license_key: str = Field(min_length=1)


class LicenseUploadResponse(BaseModel):
    status: str
    tier: str
    features: list[str]
    expires_at: str | None = None
    org_id: str | None = None


def _require_admin(principal: AuthenticatedPrincipal) -> None:
    if principal.org_role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admin users can manage licenses",
        )


def _resolve_effective_license(settings: Settings, org: Organisation | None = None) -> LicenseStatusResponse:
    """Resolve the effective license, checking org-level, then system-level (env var), then in-memory."""

    # 1. Org-level license key
    if org is not None:
        org_key = org.settings_json.get("license_key") if org.settings_json else None
        if org_key:
            validation = parse_and_verify(org_key)
            if validation.valid and validation.license_data is not None:
                d = validation.license_data
                return LicenseStatusResponse(
                    has_license=True,
                    tier=d.tier,
                    features=d.features,
                    expires_at=d.expires_at or None,
                    org_id=d.org_id or None,
                )

    # 2. In-memory store (from POST /admin/license)
    lic = get_license()
    if lic is not None:
        return LicenseStatusResponse(
            has_license=True,
            tier=lic.tier,
            features=lic.features,
            expires_at=lic.expires_at or None,
            org_id=lic.org_id or None,
        )

    # 3. System-level env var
    raw_key = getattr(settings, "modulo_license_key", "") or ""
    if raw_key:
        validation = parse_and_verify(raw_key)
        if validation.valid and validation.license_data is not None:
            d = validation.license_data
            return LicenseStatusResponse(
                has_license=True,
                tier=d.tier,
                features=d.features,
                expires_at=d.expires_at or None,
                org_id=d.org_id or None,
            )

    return LicenseStatusResponse(has_license=False)


@router.get("", response_model=LicenseStatusResponse)
async def get_license_status(
    settings: Settings = Depends(get_settings),
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> LicenseStatusResponse:
    _require_admin(current_user)

    try:
        org = None
        if current_user.organisation_id is not None:
            async with session.begin():
                org = await get_organisation(session, current_user.organisation_id)

        return _resolve_effective_license(settings, org=org)
    except ProgrammingError:
        logger.exception("license.get_failed")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="License information is not available. Run database migrations to enable this feature.",
        ) from None
    except SQLAlchemyError:
        logger.exception("license.get_failed")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database error while fetching license status.",
        ) from None


@router.post("", response_model=LicenseUploadResponse, status_code=status.HTTP_200_OK)
async def upload_license(
    req: LicenseUploadRequest,
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
) -> LicenseUploadResponse:
    _require_admin(current_user)

    try:
        validation = parse_and_verify(req.license_key)
    except LicenseError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    if not validation.valid or validation.license_data is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=validation.error or "Invalid license key",
        )

    store_license(req.license_key, validation.license_data)

    data = validation.license_data

    return LicenseUploadResponse(
        status="ok",
        tier=data.tier,
        features=data.features,
        expires_at=data.expires_at or None,
        org_id=data.org_id or None,
    )
