"""Admin feature flag inspection — lists all known flags and their current status."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response

from modulo.api.dependencies import get_db_session
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.core.feature_flags import FeatureFlagRegistry
from modulo.settings import Settings, get_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/admin/feature-flags", tags=["admin-feature-flags"])


async def _build_registry(settings: Settings, session: AsyncSession) -> FeatureFlagRegistry:
    from modulo.core.license import get_license

    lic = get_license()
    has_key = bool(settings.modulo_license_key) or lic is not None
    if lic is not None:
        tier = lic.tier
    else:
        tier = "team" if settings.modulo_license_key else "community"
    async with session.begin():
        return await FeatureFlagRegistry.from_db(
            session, current_tier=tier, has_license_key=has_key
        )


@router.get("")
async def list_feature_flags(
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_db_session),
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
) -> Response:
    try:
        registry = await _build_registry(settings, session)
        return {
            "license": {
                "tier": registry.current_tier,
                "has_license_key": registry.has_license_key,
                "is_valid": True,
            },
            "flags": [
                {
                    "name": f.name,
                    "description": f.description,
                    "tier": f.tier,
                    "currently_active": f.currently_active,
                    "depends_on": f.depends_on,
                }
                for f in registry.list_flags()
            ],
            "would_activate": [
                {
                    "name": f.name,
                    "description": f.description,
                    "tier": f.tier,
                    "depends_on": f.depends_on,
                }
                for f in registry.tier_gap_flags()
            ],
        }
    except HTTPException:
        raise
    except ProgrammingError:
        logger.exception("feature_flags.list_failed")
        return JSONResponse(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            content={
                "error": {
                    "code": "NOT_IMPLEMENTED",
                    "message": "Feature flags are not available. Run database migrations to enable this feature.",
                }
            },
        )
    except Exception:
        logger.exception("feature_flags.list_failed")
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "INTERNAL_ERROR",
                    "message": "Failed to list feature flags",
                }
            },
        )


@router.get("/{flag_name}")
async def get_feature_flag(
    flag_name: str,
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_db_session),
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
) -> Response:
    try:
        registry = await _build_registry(settings, session)
        flag = registry.get_flag(flag_name)
        if flag is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Unknown feature flag: {flag_name}",
            )
        return {
            "name": flag.name,
            "description": flag.description,
            "tier": flag.tier,
            "currently_active": flag.currently_active,
            "depends_on": flag.depends_on,
        }
    except HTTPException:
        raise
    except ProgrammingError:
        logger.exception("feature_flags.get_failed", extra={"flag_name": flag_name})
        return JSONResponse(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            content={
                "error": {
                    "code": "NOT_IMPLEMENTED",
                    "message": "Feature flags are not available. Run database migrations to enable this feature.",
                }
            },
        )
    except Exception:
        logger.exception("feature_flags.get_failed", extra={"flag_name": flag_name})
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "INTERNAL_ERROR",
                    "message": f"Failed to get feature flag: {flag_name}",
                }
            },
        )


class ToggleFlagRequest(BaseModel):
    enabled: bool


@router.put("/{flag_name}")
async def toggle_feature_flag(
    flag_name: str,
    body: ToggleFlagRequest,
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_db_session),
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
) -> Response:
    try:
        registry = await _build_registry(settings, session)
        flag = registry.get_flag(flag_name)
        if flag is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Unknown feature flag: {flag_name}",
            )
        registry.set_override(flag_name, body.enabled)
        return {
            "name": flag.name,
            "description": flag.description,
            "tier": flag.tier,
            "currently_active": flag.currently_active,
            "depends_on": flag.depends_on,
            "overridden": True,
        }
    except HTTPException:
        raise
    except ProgrammingError:
        logger.exception("feature_flags.toggle_failed", extra={"flag_name": flag_name})
        return JSONResponse(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            content={
                "error": {
                    "code": "NOT_IMPLEMENTED",
                    "message": "Feature flags are not available. Run database migrations to enable this feature.",
                }
            },
        )
    except Exception:
        logger.exception("feature_flags.toggle_failed", extra={"flag_name": flag_name})
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "INTERNAL_ERROR",
                    "message": f"Failed to toggle feature flag: {flag_name}",
                }
            },
        )
