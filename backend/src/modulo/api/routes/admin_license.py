"""Admin license endpoint — view and update the deployment license key."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.core.license import (
    LicenseError,
    get_license,
    parse_and_verify,
    store_license,
)

router = APIRouter(prefix="/api/v1/admin/license", tags=["admin-license"])


class LicenseStatusResponse(BaseModel):
    has_license: bool
    tier: str = "free"
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


@router.get("", response_model=LicenseStatusResponse)
async def get_license_status(
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
) -> LicenseStatusResponse:
    _require_admin(current_user)
    lic = get_license()
    if lic is None:
        return LicenseStatusResponse(has_license=False)
    return LicenseStatusResponse(
        has_license=True,
        tier=lic.tier,
        features=lic.features,
        expires_at=lic.expires_at or None,
        org_id=lic.org_id or None,
    )


@router.post("", response_model=LicenseUploadResponse, status_code=status.HTTP_200_OK)
async def upload_license(
    body: LicenseUploadRequest,
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
) -> LicenseUploadResponse:
    _require_admin(current_user)

    try:
        validation = parse_and_verify(body.license_key)
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

    store_license(body.license_key, validation.license_data)

    data = validation.license_data

    return LicenseUploadResponse(
        status="ok",
        tier=data.tier,
        features=data.features,
        expires_at=data.expires_at or None,
        org_id=data.org_id or None,
    )
