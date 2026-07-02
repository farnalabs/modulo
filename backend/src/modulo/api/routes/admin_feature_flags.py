"""Admin feature flag inspection — lists all known flags and their current status."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response

from modulo.api.dependencies import get_db_session
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.core.feature_flags import DbPlanContext, resolve_plan_context
from modulo.db.crud.organisation import get_organisation
from modulo.settings import Settings, get_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/admin/feature-flags", tags=["admin-feature-flags"])


async def _build_registry(settings: Settings, session: AsyncSession, current_user: AuthenticatedPrincipal) -> tuple[DbPlanContext, str, bool]:
    org = None
    if current_user.organisation_id is not None:
        org = await get_organisation(session, current_user.organisation_id)
    plan_ctx = await resolve_plan_context(settings, session, org=org)
    return plan_ctx, plan_ctx.tier(), plan_ctx.has_license_key()


@router.get("")
async def list_feature_flags(
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_db_session),
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
) -> Response:
    try:
        plan_ctx, tier, has_key = await _build_registry(settings, session, current_user)
        registry = plan_ctx._registry
        return {
            "license": {
                "tier": tier,
                "has_license_key": has_key,
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
        plan_ctx, _, _ = await _build_registry(settings, session, current_user)
        registry = plan_ctx._registry
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
        plan_ctx, _, _ = await _build_registry(settings, session, current_user)
        registry = plan_ctx._registry
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
