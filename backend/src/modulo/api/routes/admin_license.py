"""Admin license endpoint — view, update, and issue the deployment license key."""

from __future__ import annotations

import json
import logging
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.db_error_handling import handle_db_errors, raise_session_contract_error
from modulo.api.dependencies import get_db_session, require_permission, require_system_permission
from modulo.auth.dependencies import get_current_tenant_user
from modulo.auth.jwt import TenantPrincipal
from modulo.core.audit_coverage import audited
from modulo.core.feature_flags import resolve_effective_license
from modulo.core.license import (
    LicenseData,
    LicenseError,
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

#: Redis key prefix for the per-org licence-status cache (FAR-1719).
_LICENSE_CACHE_KEY_PREFIX = "license:"
#: TTL for the per-org licence-status cache, in seconds.
_LICENSE_CACHE_TTL_SECONDS = 60

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


def _resolve_effective_license(settings: Settings, org: Organisation | None = None) -> LicenseStatusResponse:
    """Resolve the effective license, checking org-level, then in-memory, then system-level (env var).

    Delegates the rung WALK to the shared session-free
    ``core.feature_flags.resolve_effective_license`` — the same walk
    ``core.feature_flags._resolve_paid_plan`` (the plan context) and
    ``GET /api/v1/plan`` use — so this admin view cannot drift from the tier
    the app actually runs on for the same deployment state.
    """
    data = resolve_effective_license(settings, org)
    if data is None:
        return LicenseStatusResponse(has_license=False, tier="community")
    return _license_status(data)


async def _read_license_cache(settings: Settings, org_id: str) -> LicenseStatusResponse | None:
    """Best-effort Redis cache read for an org's license status."""
    redis: Redis | None = None
    try:
        redis = Redis.from_url(
            settings.redis_url, decode_responses=True, socket_connect_timeout=2.0, socket_timeout=2.0
        )
        cached = await redis.get(f"{_LICENSE_CACHE_KEY_PREFIX}{org_id}")
        if cached:
            return LicenseStatusResponse(**json.loads(cached))
    except Exception:
        logger.warning("license.cache_read_failed", exc_info=True)
    finally:
        if redis is not None:
            await redis.aclose()
    return None


async def _write_license_cache(settings: Settings, org_id: str, response: LicenseStatusResponse) -> None:
    """Best-effort Redis cache write (60s TTL) for an org's license status."""
    redis: Redis | None = None
    try:
        redis = Redis.from_url(
            settings.redis_url, decode_responses=True, socket_connect_timeout=2.0, socket_timeout=2.0
        )
        await redis.set(
            f"{_LICENSE_CACHE_KEY_PREFIX}{org_id}", response.model_dump_json(), ex=_LICENSE_CACHE_TTL_SECONDS
        )
    except Exception:
        logger.warning("license.cache_write_failed", exc_info=True)
    finally:
        if redis is not None:
            await redis.aclose()


async def invalidate_license_status_cache(settings: Settings, org_id: str | None = None) -> None:
    """Best-effort invalidation of the licence-status Redis cache (FAR-1719).

    With ``org_id``, delete that org's cached entry — an org-level licence
    rotate/clear (``PUT/DELETE /api/v1/admin/orgs/{org_id}/license``) changes
    only that org's resolution. With ``org_id=None``, delete every
    ``license:*`` entry — a system-licence change (``POST /api/v1/admin/license``
    upload) can change the effective resolution of EVERY org that falls back
    to the system licence, so every cached entry is potentially stale.

    Failures are swallowed and logged: the mutation that triggered
    invalidation has already committed, and the cache expires on its own
    (60s TTL), so invalidation is best-effort and must never turn the
    committed request into a 500 (commit-then-error).
    """
    redis: Redis | None = None
    try:
        redis = Redis.from_url(
            settings.redis_url, decode_responses=True, socket_connect_timeout=2.0, socket_timeout=2.0
        )
        if org_id is not None:
            await redis.delete(f"{_LICENSE_CACHE_KEY_PREFIX}{org_id}")
            return
        batch: list[str] = []
        async for key in redis.scan_iter(match=f"{_LICENSE_CACHE_KEY_PREFIX}*", count=100):
            batch.append(key)
            if len(batch) >= 500:
                await redis.delete(*batch)
                batch.clear()
        if batch:
            await redis.delete(*batch)
    except Exception:
        logger.warning("license.cache_invalidate_failed", exc_info=True)
    finally:
        if redis is not None:
            await redis.aclose()


@router.get("")
@handle_db_errors("admin.license.get_license_status")
async def get_license_status(
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    current_user: TenantPrincipal = require_permission(_CODE_ORG_LICENSE_MANAGE),
) -> LicenseStatusResponse:

    org_id = current_user.organisation_id
    if org_id is not None:
        cached = await _read_license_cache(settings, str(org_id))
        if cached is not None:
            return cached

    try:
        org = None
        if org_id is not None:
            async with session.begin():
                org = await get_organisation(session, org_id)

        response = _resolve_effective_license(settings, org=org)
        await _write_license_cache(settings, str(org_id), response)
        return response
    except ProgrammingError:
        logger.exception(_CODE_LICENSE_GET_FAILED)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="License information is not available. Run database migrations to enable this feature.",
        ) from None
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, "admin_license.get_license_status")
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


@router.post(
    "",
    dependencies=[Depends(audited("license_uploaded", "license", principal_dep=get_current_tenant_user))],
    status_code=status.HTTP_200_OK,
)
@handle_db_errors("admin.license.upload_license")
async def upload_license(
    req: LicenseUploadRequest,
    settings: Annotated[Settings, Depends(get_settings)],
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
    # A system-licence change can flip the effective resolution of EVERY org
    # that falls back to the system licence, so bust every cached entry. The
    # upload above already committed to in-memory state; a cache failure must
    # not turn it into a 500 (best-effort inside the helper, FAR-1719).
    await invalidate_license_status_cache(settings)

    data = validation.license_data

    return LicenseUploadResponse(
        status="ok",
        tier=data.tier,
        features=data.features,
        expires_at=data.expires_at or None,
        org_id=data.org_id or None,
    )


@router.post(
    "/issue",
    dependencies=[Depends(audited("license_issued", "license", principal_dep=get_current_tenant_user))],
    status_code=status.HTTP_201_CREATED,
)
@handle_db_errors("admin.license.issue_license")
async def issue_license(
    req: LicenseIssueRequest,
    background_tasks: BackgroundTasks,
    settings: Annotated[Settings, Depends(get_settings)],
    _: TenantPrincipal = require_system_permission("system.config.manage"),  # type: ignore[assignment]
) -> LicenseIssueResponse:
    """Manually issue (sign) a team license key for a customer.

    Uses the same signing service as the Stripe purchase fulfilment webhook.
    When ``email`` is provided, the license key is also emailed to the customer
    via a background task so this request stays fast.
    """
    license_key = generate_team_license(
        req.org_name,
        term_months=req.term_months,
        features=req.features if req.features is not None else TEAM_FEATURES,
        private_key_hex=settings.modulo_license_private_key or None,
    )

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
