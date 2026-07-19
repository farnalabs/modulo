"""Admin feature flag inspection — lists all known flags and their current status."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response

from modulo.api.db_error_handling import handle_db_errors
from modulo.api.dependencies import get_db_session
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.core.feature_flags import FeatureFlagRegistry
from modulo.core.license import get_license, parse_and_verify
from modulo.db.crud.organisation import get_organisation
from modulo.settings import Settings, get_settings

logger = logging.getLogger(__name__)

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/admin/feature-flags", tags=["admin-feature-flags"])


def _require_admin(principal: AuthenticatedPrincipal) -> None:
    if not principal.is_system_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")


async def _resolve_tier(settings: Settings, session: AsyncSession, current_user: AuthenticatedPrincipal) -> str:
    """Resolve the effective tier for the current user's org.

    Resolution order:
    1. Org-level license key (from org.settings_json["license_key"])
    2. System-level in-memory license (store_license())
    3. System-level env-var license (settings.modulo_license_key)
    4. Org.plan_id (per-org, from DB)
    5. Community fallback
    """
    org = None
    if current_user.organisation_id is not None:
        async with session.begin():
            org = await get_organisation(session, current_user.organisation_id)

    # 1. Org-level license key
    if org is not None:
        org_settings = getattr(org, "settings_json", None)
        org_license_key = org_settings.get("license_key") if isinstance(org_settings, dict) else None
        if org_license_key:
            try:
                validation = parse_and_verify(org_license_key)
                if validation.valid and validation.license_data is not None:
                    return validation.license_data.tier
            except Exception:
                logger.warning("Failed to parse org-level license key", exc_info=True)

    # 2. System-level in-memory license
    lic = get_license()
    if lic is not None:
        return lic.tier

    # 3. System-level env-var license
    raw_key: str = getattr(settings, "modulo_license_key", "") or ""
    if raw_key:
        try:
            validation = parse_and_verify(raw_key)
            if validation.valid and validation.license_data is not None:
                return validation.license_data.tier
        except Exception:
            logger.warning("Failed to parse env-var license key", exc_info=True)

    # 4. Org-level plan_id
    if org is not None:
        org_plan_id: str | None = getattr(org, "plan_id", None)
        if org_plan_id:
            return org_plan_id

    # 5. Community fallback
    return "community"


async def _build_registry(
    settings: Settings, session: AsyncSession, current_user: AuthenticatedPrincipal
) -> FeatureFlagRegistry:
    tier = await _resolve_tier(settings, session, current_user)
    lic = get_license()
    has_key = bool(settings.modulo_license_key) or lic is not None

    if not has_key and current_user.organisation_id is not None:
        async with session.begin():
            org = await get_organisation(session, current_user.organisation_id)
            if org is not None and isinstance(getattr(org, "settings_json", None), dict):
                has_key = bool(org.settings_json.get("license_key"))

    async with session.begin():
        return await FeatureFlagRegistry.from_db(
            session,
            current_tier=tier,
            has_license_key=has_key,
        )


@handle_db_errors("admin.feature_flags.list_feature_flags")
@router.get("", response_model=None)
async def list_feature_flags(
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_db_session),
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
) -> Response | dict[str, Any]:
    try:
        registry = await _build_registry(settings, session, current_user)
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
    except SQLAlchemyError:
        logger.exception("feature_flags.list_failed")
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "error": {
                    "code": "SERVICE_UNAVAILABLE",
                    "message": "Database error while listing feature flags.",
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


@handle_db_errors("admin.feature_flags.get_feature_flag")
@router.get("/{flag_name}", response_model=None)
async def get_feature_flag(
    flag_name: str,
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_db_session),
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
) -> Response | dict[str, Any]:
    try:
        registry = await _build_registry(settings, session, current_user)
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
    except SQLAlchemyError:
        logger.exception("feature_flags.get_failed", extra={"flag_name": flag_name})
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "error": {
                    "code": "SERVICE_UNAVAILABLE",
                    "message": "Database error while getting feature flag.",
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


@handle_db_errors("admin.feature_flags.toggle_feature_flag")
@router.put("/{flag_name}", response_model=None)
async def toggle_feature_flag(
    flag_name: str,
    req: ToggleFlagRequest,
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_db_session),
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
) -> Response | dict[str, Any]:
    try:
        registry = await _build_registry(settings, session, current_user)
        flag = registry.get_flag(flag_name)
        if flag is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Unknown feature flag: {flag_name}",
            )
        registry.set_override(flag_name, req.enabled)
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
    except SQLAlchemyError:
        logger.exception("feature_flags.toggle_failed", extra={"flag_name": flag_name})
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "error": {
                    "code": "SERVICE_UNAVAILABLE",
                    "message": "Database error while toggling feature flag.",
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


@handle_db_errors("admin.feature_flags.get_org_flag_override")
@router.get("/{flag_name}/org-override", response_model=None)
async def get_org_flag_override(
    flag_name: str,
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> Response | dict[str, Any]:
    _require_admin(current_user)
    assert current_user.organisation_id is not None
    try:
        async with session.begin():
            org = await get_organisation(session, current_user.organisation_id)
        if not org or not org.settings_json:
            return {"override": None}
        overrides = org.settings_json.get("feature_overrides", {})
        return {"override": overrides.get(flag_name)}
    except HTTPException:
        raise
    except ProgrammingError:
        logger.exception("feature_flags.get_org_override_failed", extra={"flag_name": flag_name})
        return JSONResponse(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            content={
                "error": {
                    "code": "NOT_IMPLEMENTED",
                    "message": "Feature flags are not available. Run database migrations to enable this feature.",
                }
            },
        )
    except SQLAlchemyError:
        logger.exception("feature_flags.get_org_override_failed", extra={"flag_name": flag_name})
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "error": {
                    "code": "SERVICE_UNAVAILABLE",
                    "message": "Database error while fetching org flag override.",
                }
            },
        )
    except Exception:
        logger.exception("feature_flags.get_org_override_failed", extra={"flag_name": flag_name})
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "INTERNAL_ERROR",
                    "message": "Failed to get org flag override.",
                }
            },
        )


@handle_db_errors("admin.feature_flags.set_org_flag_override")
@router.put("/{flag_name}/org-override", response_model=None)
async def set_org_flag_override(
    flag_name: str,
    req: ToggleFlagRequest,
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> Response | dict[str, Any]:
    _require_admin(current_user)
    assert current_user.organisation_id is not None
    try:
        async with session.begin():
            org = await get_organisation(session, current_user.organisation_id)
            if not org:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "Org not found")
            settings_dict = dict(org.settings_json or {})
            overrides = dict(settings_dict.get("feature_overrides", {}))
            overrides[flag_name] = req.enabled
            settings_dict["feature_overrides"] = overrides
            org.settings_json = settings_dict
            session.add(org)
        return {"override": req.enabled}
    except HTTPException:
        raise
    except ProgrammingError:
        logger.exception("feature_flags.set_org_override_failed", extra={"flag_name": flag_name})
        return JSONResponse(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            content={
                "error": {
                    "code": "NOT_IMPLEMENTED",
                    "message": "Feature flags are not available. Run database migrations to enable this feature.",
                }
            },
        )
    except SQLAlchemyError:
        logger.exception("feature_flags.set_org_override_failed", extra={"flag_name": flag_name})
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "error": {
                    "code": "SERVICE_UNAVAILABLE",
                    "message": "Database error while setting org flag override.",
                }
            },
        )
    except Exception:
        logger.exception("feature_flags.set_org_override_failed", extra={"flag_name": flag_name})
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "INTERNAL_ERROR",
                    "message": "Failed to set org flag override.",
                }
            },
        )


@handle_db_errors("admin.feature_flags.clear_org_flag_override")
@router.delete("/{flag_name}/org-override", response_model=None)
async def clear_org_flag_override(
    flag_name: str,
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> Response | dict[str, Any]:
    _require_admin(current_user)
    assert current_user.organisation_id is not None
    try:
        async with session.begin():
            org = await get_organisation(session, current_user.organisation_id)
            if not org or not org.settings_json:
                return {"override": None}
            settings_dict = dict(org.settings_json)
            overrides = dict(settings_dict.get("feature_overrides", {}))
            overrides.pop(flag_name, None)
            settings_dict["feature_overrides"] = overrides
            org.settings_json = settings_dict
            session.add(org)
        return {"override": None}
    except HTTPException:
        raise
    except ProgrammingError:
        logger.exception("feature_flags.clear_org_override_failed", extra={"flag_name": flag_name})
        return JSONResponse(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            content={
                "error": {
                    "code": "NOT_IMPLEMENTED",
                    "message": "Feature flags are not available. Run database migrations to enable this feature.",
                }
            },
        )
    except SQLAlchemyError:
        logger.exception("feature_flags.clear_org_override_failed", extra={"flag_name": flag_name})
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "error": {
                    "code": "SERVICE_UNAVAILABLE",
                    "message": "Database error while clearing org flag override.",
                }
            },
        )
    except Exception:
        logger.exception("feature_flags.clear_org_override_failed", extra={"flag_name": flag_name})
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "INTERNAL_ERROR",
                    "message": "Failed to clear org flag override.",
                }
            },
        )
