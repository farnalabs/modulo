"""Admin feature flag inspection — lists all known flags and their current status."""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response

from modulo.api.db_error_handling import handle_db_errors
from modulo.api.dependencies import get_db_session, require_system_permission
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.core.feature_flags import FeatureFlagRegistry, resolve_plan_context
from modulo.core.license import get_license
from modulo.db.crud.organisation import get_organisation
from modulo.settings import Settings, get_settings

_CODE_FEATURE_FLAGS_LIST_FAILED = "feature_flags.list_failed"
_MSG_FEATURE_FLAGS_NOT_AVAILABLE = "Feature flags are not available. Run database migrations to enable this feature."
_CODE_FEATURE_FLAGS_GET_FAILED = "feature_flags.get_failed"
_CODE_SYSTEM_CONFIG_MANAGE = "system.config.manage"
_CODE_FEATURE_FLAGS_TOGGLE_FAILED = "feature_flags.toggle_failed"
_CODE_FEATURE_FLAGS_GET_ORG = "feature_flags.get_org_override_failed"
_CODE_FEATURE_FLAGS_SET_ORG = "feature_flags.set_org_override_failed"
_CODE_FEATURE_FLAGS_CLEAR_ORG = "feature_flags.clear_org_override_failed"


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/admin/feature-flags", tags=["admin-feature-flags"])


async def _resolve_tier(settings: Settings, session: AsyncSession, current_user: AuthenticatedPrincipal) -> str:
    """Resolve the effective tier for the current user's org.

    Delegates to ``resolve_plan_context`` — the same license-gated resolution
    used by the API plan-context dependency — so the frontend tier path (this
    endpoint powers the UI plan store) cannot bypass licensing. A bare
    non-community ``plan_id`` with no valid signed license resolves to
    community instead of granting the paid tier.
    """
    org = None
    if current_user.organisation_id is not None:
        async with session.begin():
            org = await get_organisation(session, current_user.organisation_id)

    plan_context = await resolve_plan_context(settings, session, org)
    return plan_context.tier()


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


async def _invalidate_cache(settings: Settings, org_id: str | uuid.UUID) -> None:
    """Best-effort delete of the ``list_feature_flags`` Redis cache for an org.

    The cache stores a 60s-TTL payload that overlays the org's
    ``feature_overrides``. An admin toggling an org override must see it take
    effect app-wide immediately; a stale cached payload would mask the change
    for up to 60s. Failures are swallowed and logged — the cache expires on its
    own, so invalidation is best-effort.
    """
    redis: Redis | None = None
    try:
        redis = Redis.from_url(
            settings.redis_url, decode_responses=True, socket_connect_timeout=2.0, socket_timeout=2.0
        )
        await redis.delete(f"feature-flags:{org_id}")
    except Exception:
        logger.warning("feature-flags.cache_invalidate_failed", exc_info=True)
    finally:
        if redis is not None:
            await redis.aclose()


@router.get("", response_model=None)
@handle_db_errors("admin.feature_flags.list_feature_flags")
async def list_feature_flags(
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_db_session),
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
) -> Response | dict[str, Any]:
    # Attempt Redis cache read
    redis: Redis | None = None
    try:
        redis = Redis.from_url(
            settings.redis_url, decode_responses=True, socket_connect_timeout=2.0, socket_timeout=2.0
        )
        cache_key = f"feature-flags:{current_user.organisation_id}"
        cached = await redis.get(cache_key)
        if cached:
            return cast("dict[str, Any]", json.loads(cached))
    except Exception:
        logger.warning("feature-flags.cache_read_failed", exc_info=True)
    finally:
        if redis is not None:
            await redis.aclose()

    try:
        registry = await _build_registry(settings, session, current_user)

        # Apply the org's per-flag overrides to the payload so an org-level
        # enable (admin Feature Flags UI) takes effect app-wide — the whole app
        # (plan store) reads ``currently_active`` from this endpoint. The admin
        # UI's org-override toggle persists into
        # ``org.settings_json.feature_overrides``; overlay that here on the
        # registry's computed default. Best-effort: a failed org read falls back
        # to the registry's defaults rather than failing the request.
        org_overrides: dict[str, bool] = {}
        if current_user.organisation_id is not None:
            try:
                async with session.begin():
                    org = await get_organisation(session, current_user.organisation_id)
                if org is not None and isinstance(getattr(org, "settings_json", None), dict):
                    org_overrides = {
                        key: bool(value)
                        for key, value in org.settings_json.get("feature_overrides", {}).items()
                        if isinstance(value, bool)
                    }
            except Exception:
                logger.warning("feature-flags.org_override_read_failed", exc_info=True)

        response_data = {
            "license": {
                "tier": registry.current_tier,
                "has_license_key": registry.has_license_key,
                "is_valid": True,
            },
            "dev_mode": settings.modulo_dev_mode,
            "flags": [
                {
                    "name": f.name,
                    "description": f.description,
                    "tier": f.tier,
                    "currently_active": org_overrides.get(f.name, f.currently_active),
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
                if f.name not in org_overrides
            ],
        }

        # Write to Redis cache (best-effort, 60s TTL)
        try:
            redis = Redis.from_url(
                settings.redis_url, decode_responses=True, socket_connect_timeout=2.0, socket_timeout=2.0
            )
            cache_key = f"feature-flags:{current_user.organisation_id}"
            await redis.setex(cache_key, 60, json.dumps(response_data, default=str))
        except Exception:
            logger.warning("feature-flags.cache_write_failed", exc_info=True)
        finally:
            if redis is not None:
                await redis.aclose()

        return response_data
    except HTTPException:
        raise
    except ProgrammingError:
        logger.exception(_CODE_FEATURE_FLAGS_LIST_FAILED)
        return JSONResponse(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            content={
                "error": {
                    "code": "NOT_IMPLEMENTED",
                    "message": _MSG_FEATURE_FLAGS_NOT_AVAILABLE,
                }
            },
        )
    except SQLAlchemyError:
        logger.exception(_CODE_FEATURE_FLAGS_LIST_FAILED)
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
        logger.exception(_CODE_FEATURE_FLAGS_LIST_FAILED)
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "INTERNAL_ERROR",
                    "message": "Failed to list feature flags",
                }
            },
        )


@router.get("/{flag_name}", response_model=None)
@handle_db_errors("admin.feature_flags.get_feature_flag")
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
        logger.exception(_CODE_FEATURE_FLAGS_GET_FAILED, extra={"flag_name": flag_name})
        return JSONResponse(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            content={
                "error": {
                    "code": "NOT_IMPLEMENTED",
                    "message": _MSG_FEATURE_FLAGS_NOT_AVAILABLE,
                }
            },
        )
    except SQLAlchemyError:
        logger.exception(_CODE_FEATURE_FLAGS_GET_FAILED, extra={"flag_name": flag_name})
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
        logger.exception(_CODE_FEATURE_FLAGS_GET_FAILED, extra={"flag_name": flag_name})
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


@router.put("/{flag_name}", response_model=None)
@handle_db_errors("admin.feature_flags.toggle_feature_flag")
async def toggle_feature_flag(
    flag_name: str,
    req: ToggleFlagRequest,
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_db_session),
    current_user: AuthenticatedPrincipal = require_system_permission(_CODE_SYSTEM_CONFIG_MANAGE),  # type: ignore[assignment]
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
        logger.exception(_CODE_FEATURE_FLAGS_TOGGLE_FAILED, extra={"flag_name": flag_name})
        return JSONResponse(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            content={
                "error": {
                    "code": "NOT_IMPLEMENTED",
                    "message": _MSG_FEATURE_FLAGS_NOT_AVAILABLE,
                }
            },
        )
    except SQLAlchemyError:
        logger.exception(_CODE_FEATURE_FLAGS_TOGGLE_FAILED, extra={"flag_name": flag_name})
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
        logger.exception(_CODE_FEATURE_FLAGS_TOGGLE_FAILED, extra={"flag_name": flag_name})
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "INTERNAL_ERROR",
                    "message": f"Failed to toggle feature flag: {flag_name}",
                }
            },
        )


@router.get("/{flag_name}/org-override", response_model=None)
@handle_db_errors("admin.feature_flags.get_org_flag_override")
async def get_org_flag_override(
    flag_name: str,
    current_user: AuthenticatedPrincipal = require_system_permission(_CODE_SYSTEM_CONFIG_MANAGE),  # type: ignore[assignment]
    session: AsyncSession = Depends(get_db_session),
) -> Response | dict[str, Any]:
    assert current_user.organisation_id is not None  # nosec B101 -- genuine invariant: require_system_permission guarantees a non-None organisation_id for org-scoped system-config routes
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
        logger.exception(_CODE_FEATURE_FLAGS_GET_ORG, extra={"flag_name": flag_name})
        return JSONResponse(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            content={
                "error": {
                    "code": "NOT_IMPLEMENTED",
                    "message": _MSG_FEATURE_FLAGS_NOT_AVAILABLE,
                }
            },
        )
    except SQLAlchemyError:
        logger.exception(_CODE_FEATURE_FLAGS_GET_ORG, extra={"flag_name": flag_name})
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
        logger.exception(_CODE_FEATURE_FLAGS_GET_ORG, extra={"flag_name": flag_name})
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "INTERNAL_ERROR",
                    "message": "Failed to get org flag override.",
                }
            },
        )


@router.put("/{flag_name}/org-override", response_model=None)
@handle_db_errors("admin.feature_flags.set_org_flag_override")
async def set_org_flag_override(
    flag_name: str,
    req: ToggleFlagRequest,
    settings: Settings = Depends(get_settings),
    current_user: AuthenticatedPrincipal = require_system_permission(_CODE_SYSTEM_CONFIG_MANAGE),  # type: ignore[assignment]
    session: AsyncSession = Depends(get_db_session),
) -> Response | dict[str, Any]:
    assert current_user.organisation_id is not None  # nosec B101 -- genuine invariant: require_system_permission guarantees a non-None organisation_id for org-scoped system-config routes
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
        await _invalidate_cache(settings, current_user.organisation_id)
        return {"override": req.enabled}
    except HTTPException:
        raise
    except ProgrammingError:
        logger.exception(_CODE_FEATURE_FLAGS_SET_ORG, extra={"flag_name": flag_name})
        return JSONResponse(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            content={
                "error": {
                    "code": "NOT_IMPLEMENTED",
                    "message": _MSG_FEATURE_FLAGS_NOT_AVAILABLE,
                }
            },
        )
    except SQLAlchemyError:
        logger.exception(_CODE_FEATURE_FLAGS_SET_ORG, extra={"flag_name": flag_name})
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
        logger.exception(_CODE_FEATURE_FLAGS_SET_ORG, extra={"flag_name": flag_name})
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "INTERNAL_ERROR",
                    "message": "Failed to set org flag override.",
                }
            },
        )


@router.delete("/{flag_name}/org-override", response_model=None)
@handle_db_errors("admin.feature_flags.clear_org_flag_override")
async def clear_org_flag_override(
    flag_name: str,
    settings: Settings = Depends(get_settings),
    current_user: AuthenticatedPrincipal = require_system_permission(_CODE_SYSTEM_CONFIG_MANAGE),  # type: ignore[assignment]
    session: AsyncSession = Depends(get_db_session),
) -> Response | dict[str, Any]:
    assert current_user.organisation_id is not None  # nosec B101 -- genuine invariant: require_system_permission guarantees a non-None organisation_id for org-scoped system-config routes
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
        await _invalidate_cache(settings, current_user.organisation_id)
        return {"override": None}
    except HTTPException:
        raise
    except ProgrammingError:
        logger.exception(_CODE_FEATURE_FLAGS_CLEAR_ORG, extra={"flag_name": flag_name})
        return JSONResponse(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            content={
                "error": {
                    "code": "NOT_IMPLEMENTED",
                    "message": _MSG_FEATURE_FLAGS_NOT_AVAILABLE,
                }
            },
        )
    except SQLAlchemyError:
        logger.exception(_CODE_FEATURE_FLAGS_CLEAR_ORG, extra={"flag_name": flag_name})
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
        logger.exception(_CODE_FEATURE_FLAGS_CLEAR_ORG, extra={"flag_name": flag_name})
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "INTERNAL_ERROR",
                    "message": "Failed to clear org flag override.",
                }
            },
        )
