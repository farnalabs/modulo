"""Admin license endpoint — view, update, and issue the deployment license key."""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.db_error_handling import handle_db_errors
from modulo.api.dependencies import get_db_session, require_permission, require_system_permission
from modulo.auth.jwt import TenantPrincipal
from modulo.core.license import (
    LicenseData,
    LicenseError,
    get_license,
    parse_and_verify,
    store_license,
)
from modulo.core.license_signing import TEAM_FEATURES, generate_team_license
from modulo.core.stripe_fulfilment import email_team_license
from modulo.db.crud.organisation import get_organisation
from modulo.db.models.organisation import Organisation
from modulo.settings import Settings, get_settings

_CODE_ORG_LICENSE_MANAGE = "org.license.manage"
_CODE_LICENSE_GET_FAILED = "license.get_failed"


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


class LicenseIssueRequest(BaseModel):
    org_name: str = Field(min_length=1, max_length=200)
    term_months: int = Field(default=12, ge=1, le=120)
    features: list[str] | None = None
    email: str | None = None


class LicenseIssueResponse(BaseModel):
    license_key: str
    expires_at: str
    org_name: str
    tier: str
    features: list[str]
    org_id: str


def _license_status(data: LicenseData, *, has_license: bool = True) -> LicenseStatusResponse:
    """Build a ``LicenseStatusResponse`` from a signed-license payload."""
    return LicenseStatusResponse(
        has_license=has_license,
        tier=data.tier,
        features=data.features,
        expires_at=data.expires_at or None,
        org_id=data.org_id or None,
    )


def _resolve_license_key(raw_key: str) -> LicenseStatusResponse | None:
    """Validate ``raw_key`` and return its license status, or ``None`` if unusable."""
    if not raw_key:
        return None
    validation = parse_and_verify(raw_key)
    if not validation.valid or validation.license_data is None:
        return None
    return _license_status(validation.license_data)


def _resolve_effective_license(settings: Settings, org: Organisation | None = None) -> LicenseStatusResponse:
    """Resolve the effective license, checking org-level, then system-level (env var), then in-memory."""
    # 1. Org-level license key
    if org is not None:
        org_key = org.settings_json.get("license_key") if org.settings_json else None
        resolved = _resolve_license_key(str(org_key) if org_key else "")
        if resolved is not None:
            return resolved

    # 2. In-memory store (from POST /admin/license)
    lic = get_license()
    if lic is not None:
        return _license_status(lic)

    # 3. System-level env var
    resolved = _resolve_license_key(getattr(settings, "modulo_license_key", "") or "")
    if resolved is not None:
        return resolved

    return LicenseStatusResponse(has_license=False, tier="community")


@router.get("")
@handle_db_errors("admin.license.get_license_status")
async def get_license_status(
    settings: Settings = Depends(get_settings),
    current_user: TenantPrincipal = require_permission(_CODE_ORG_LICENSE_MANAGE),
    session: AsyncSession = Depends(get_db_session),
) -> LicenseStatusResponse:

    # Attempt Redis cache read
    redis: Redis | None = None
    try:
        redis = Redis.from_url(
            settings.redis_url, decode_responses=True, socket_connect_timeout=2.0, socket_timeout=2.0
        )
        cache_key = f"license:{current_user.organisation_id}"
        cached = await redis.get(cache_key)
        if cached:
            return LicenseStatusResponse(**json.loads(cached))
    except Exception:
        logger.warning("license.cache_read_failed", exc_info=True)
    finally:
        if redis is not None:
            await redis.aclose()

    try:
        org = None
        if current_user.organisation_id is not None:
            async with session.begin():
                org = await get_organisation(session, current_user.organisation_id)

        response = _resolve_effective_license(settings, org=org)

        # Write to Redis cache (best-effort, 60s TTL)
        try:
            redis = Redis.from_url(
                settings.redis_url, decode_responses=True, socket_connect_timeout=2.0, socket_timeout=2.0
            )
            cache_key = f"license:{current_user.organisation_id}"
            await redis.setex(cache_key, 60, response.model_dump_json())
        except Exception:
            logger.warning("license.cache_write_failed", exc_info=True)
        finally:
            if redis is not None:
                await redis.aclose()

        return response
    except ProgrammingError:
        logger.exception(_CODE_LICENSE_GET_FAILED)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="License information is not available. Run database migrations to enable this feature.",
        ) from None
    except SQLAlchemyError:
        logger.exception(_CODE_LICENSE_GET_FAILED)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database error while fetching license status.",
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception(_CODE_LICENSE_GET_FAILED)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve license status.",
        ) from None


@router.post("", status_code=status.HTTP_200_OK)
@handle_db_errors("admin.license.upload_license")
async def upload_license(
    req: LicenseUploadRequest,
    _: TenantPrincipal = require_system_permission("system.config.manage"),  # type: ignore[assignment]
) -> LicenseUploadResponse:
    try:
        validation = parse_and_verify(req.license_key)
    except LicenseError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    if not validation.valid or validation.license_data is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
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


@router.post("/issue", status_code=status.HTTP_201_CREATED)
@handle_db_errors("admin.license.issue_license")
async def issue_license(
    req: LicenseIssueRequest,
    background_tasks: BackgroundTasks,
    settings: Settings = Depends(get_settings),
    _: TenantPrincipal = require_system_permission("system.config.manage"),  # type: ignore[assignment]
) -> LicenseIssueResponse:
    """Manually issue (sign) a team license key for a customer.

    Uses the same signing service as the Stripe purchase fulfilment webhook.
    When ``email`` is provided, the license key is also emailed to the customer
    via a background task so this request stays fast.
    """
    try:
        license_key = generate_team_license(
            req.org_name,
            term_months=req.term_months,
            features=req.features if req.features is not None else TEAM_FEATURES,
            private_key_hex=settings.modulo_license_private_key or None,
        )
    except ValueError as exc:
        logger.exception("license.issue_generation_failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc

    validation = parse_and_verify(license_key)
    if not validation.valid or validation.license_data is None:
        logger.error("license.issue_verification_failed error=%s", validation.error)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Generated license key failed signature verification.",
        ) from None

    data = validation.license_data

    if req.email:
        background_tasks.add_task(
            email_team_license,
            settings,
            req.email,
            license_key,
            data.expires_at,
        )

    return LicenseIssueResponse(
        license_key=license_key,
        expires_at=data.expires_at,
        org_name=req.org_name,
        tier=data.tier,
        features=data.features,
        org_id=data.org_id,
    )
