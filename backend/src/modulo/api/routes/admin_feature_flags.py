"""Admin feature flag inspection — list, inspect, toggle, and org-override feature flags.

Toggles and org overrides persist into the caller's organisation settings
(``org.settings_json.feature_overrides``) so a change survives a fresh
request/process; the registry's computed tier state is only the default that
org overrides overlay on top of (both GET endpoints apply the overlay).
"""

from __future__ import annotations

import asyncio
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
from modulo.auth.jwt import AuthenticatedPrincipal, TenantPrincipal
from modulo.core.audit_logger import append_audit_event_isolated
from modulo.core.feature_flags import FeatureFlagRegistry, resolve_plan_context
from modulo.core.license import get_license
from modulo.db.crud.organisation import get_organisation
from modulo.settings import Settings, get_settings

_AUDIT_EVENT_FLAG_OVERRIDE_SET = "feature_flag_override_set"
_AUDIT_EVENT_FLAG_OVERRIDE_CLEARED = "feature_flag_override_cleared"
_AUDIT_LOG_KEY = "feature_flags.audit_append_failed"

_CODE_FEATURE_FLAGS_LIST_FAILED = "feature_flags.list_failed"
_MSG_FEATURE_FLAGS_NOT_AVAILABLE = "Feature flags are not available. Run database migrations to enable this feature."
_CODE_FEATURE_FLAGS_GET_FAILED = "feature_flags.get_failed"
_CODE_SYSTEM_CONFIG_MANAGE = "system.config.manage"
_CODE_FEATURE_FLAGS_TOGGLE_FAILED = "feature_flags.toggle_failed"
_CODE_FEATURE_FLAGS_GET_ORG = "feature_flags.get_org_override_failed"
_CODE_FEATURE_FLAGS_SET_ORG = "feature_flags.set_org_override_failed"
_CODE_FEATURE_FLAGS_CLEAR_ORG = "feature_flags.clear_org_override_failed"
_MSG_ORG_ID_REQUIRED = "Organisation ID required for org-scoped feature flag operations"


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


async def _read_org_overrides(session: AsyncSession, org_id: uuid.UUID) -> dict[str, bool]:
    """Read the org's ``feature_overrides`` map (bool entries only).

    Returns ``{}`` when the org or its settings are missing. Callers own the
    error policy: both GET endpoints treat a failed read as best-effort and
    fall back to the registry defaults.
    """
    async with session.begin():
        org = await get_organisation(session, org_id)
    if org is None or not isinstance(getattr(org, "settings_json", None), dict):
        return {}
    return {
        key: bool(value)
        for key, value in org.settings_json.get("feature_overrides", {}).items()
        if isinstance(value, bool)
    }


async def _write_org_override(session: AsyncSession, org_id: uuid.UUID, flag_name: str, enabled: bool) -> None:
    """Persist ``feature_overrides[flag_name] = enabled`` in the org's settings.

    The single durable write path shared by the toggle (``PUT /{flag_name}``)
    and org-override (``PUT /{flag_name}/org-override``) endpoints, so both
    endpoints write the same truth. Raises 404 when the org does not exist.
    """
    async with session.begin():
        org = await get_organisation(session, org_id)
        if not org:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Org not found")
        settings_dict = dict(org.settings_json or {})
        overrides = dict(settings_dict.get("feature_overrides", {}))
        overrides[flag_name] = enabled
        settings_dict["feature_overrides"] = overrides
        org.settings_json = settings_dict
        session.add(org)


def _enforce_team_tier_gate(
    flag: Any,
    flag_name: str,
    registry: FeatureFlagRegistry,
    enabled: bool,
) -> None:
    """Reject attempts to ENABLE a team-tier flag when the org is not on the team tier.

    System-level overrides (set_override) remain permitted — this gate only
    applies to org-level write paths (toggle and org-override endpoints).
    Ranks come from the same registry whose tier produced the flag's
    ``currently_active``, so a DB-catalog tier that outranks the caller's
    current tier is rejected even when it is absent from the hardcoded
    ``TIER_RANK`` map. Raises 403 when the org cannot enable this flag.
    """
    if not enabled:
        return
    current_tier = registry.current_tier
    if registry.tier_rank(flag.tier) > registry.tier_rank(current_tier):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Cannot enable team-tier flag '{flag_name}' on the '{current_tier}' plan. A team licence is required."
            ),
        )


async def _apply_org_flag_override(
    settings: Settings,
    session: AsyncSession,
    current_user: AuthenticatedPrincipal,
    flag_name: str,
    enabled: bool,
) -> Any:
    """Validate and persist an org-level flag override, returning the flag.

    Shared by the toggle (``PUT /{flag_name}``) and org-override
    (``PUT /{flag_name}/org-override``) endpoints so both apply the same
    org-id check, flag-existence check, team-tier gate, durable write and
    cache invalidation.
    """
    if current_user.organisation_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=_MSG_ORG_ID_REQUIRED,
        )
    registry = await _build_registry(settings, session, current_user)
    flag = registry.get_flag(flag_name)
    if flag is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown feature flag: {flag_name}",
        )
    _enforce_team_tier_gate(flag, flag_name, registry, enabled)
    await _write_org_override(session, current_user.organisation_id, flag_name, enabled)
    await _invalidate_cache(settings, current_user.organisation_id)
    return flag


async def _emit_org_flag_override_audit(
    session: AsyncSession,
    current_user: AuthenticatedPrincipal,
    *,
    flag_name: str,
    enabled: bool | None,
) -> None:
    """Record an org feature-flag override change on the tamper-evident audit chain.

    Org-level feature-flag governance (which flag the org overrode, in which
    direction, by whom, and when) must be auditable — the kill-switch pause and
    org-user lifecycle precedents both write audit events, and org overrides
    change what the whole org can run, so a silent ``feature_overrides`` write
    would make governance unattributable. ``enabled=None`` means the override
    was cleared; ``True``/``False`` means it was set to that value.

    Fail-open by design (the ``append_audit_event_isolated`` contract): the
    override has already committed and must never roll back because the audit
    write failed — a broken append is logged under ``_AUDIT_LOG_KEY`` and the
    change stands. No-op when the principal carries no organisation (that path
    is 403'd upstream, but the helper must never be the thing that raises).
    """
    if current_user.organisation_id is None:
        return
    principal = TenantPrincipal(
        username=current_user.username,
        organisation_id=current_user.organisation_id,
        account_id=current_user.account_id,
        org_role=current_user.org_role or "admin",
        is_system_admin=current_user.is_system_admin,
        via_api_key=current_user.via_api_key,
        client_kind=current_user.client_kind,
    )
    payload: dict[str, Any] = {"flag_name": flag_name}
    if enabled is not None:
        payload["enabled"] = bool(enabled)
        event_type = _AUDIT_EVENT_FLAG_OVERRIDE_SET
    else:
        event_type = _AUDIT_EVENT_FLAG_OVERRIDE_CLEARED
    try:
        await append_audit_event_isolated(
            session,
            principal,
            resource_type="org",
            resource_id=current_user.organisation_id,
            event_type=event_type,
            payload=payload,
            log_key=_AUDIT_LOG_KEY,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning(_AUDIT_LOG_KEY, exc_info=True)


@router.get("", response_model=None)
@handle_db_errors("admin.feature_flags.list_feature_flags")
async def list_feature_flags(
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_db_session),
    current_user: AuthenticatedPrincipal = require_system_permission(_CODE_SYSTEM_CONFIG_MANAGE),  # type: ignore[assignment]
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
                org_overrides = await _read_org_overrides(session, current_user.organisation_id)
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
        # Overlay the caller's org ``feature_overrides`` on the registry default
        # so this endpoint reports the SAME effective ``currently_active`` as
        # the list endpoint (which the frontend consumes). Best-effort: a
        # failed org read falls back to the registry default.
        currently_active = flag.currently_active
        if current_user.organisation_id is not None:
            try:
                org_overrides = await _read_org_overrides(session, current_user.organisation_id)
            except Exception:
                logger.warning("feature-flags.org_override_read_failed", exc_info=True)
            else:
                currently_active = org_overrides.get(flag.name, flag.currently_active)
        return {
            "name": flag.name,
            "description": flag.description,
            "tier": flag.tier,
            "currently_active": currently_active,
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
                    "message": "Failed to get feature flag.",
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
    """Toggle a feature flag for the caller's organisation — persists durably.

    Writes ``feature_overrides[flag_name]`` into the org's settings (the same
    persistence path as ``PUT /{flag_name}/org-override``) and invalidates the
    list cache, so the toggle survives a fresh request/process and both
    endpoints write the same truth. ``overridden: true`` is only returned
    after the durable write has committed.
    """
    try:
        flag = await _apply_org_flag_override(settings, session, current_user, flag_name, req.enabled)
        await _emit_org_flag_override_audit(
            session,
            current_user,
            flag_name=flag_name,
            enabled=req.enabled,
        )
        return {
            "name": flag.name,
            "description": flag.description,
            "tier": flag.tier,
            "currently_active": req.enabled,
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
                    "message": "Failed to toggle feature flag.",
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
    if current_user.organisation_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=_MSG_ORG_ID_REQUIRED,
        )
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
    try:
        await _apply_org_flag_override(settings, session, current_user, flag_name, req.enabled)
        await _emit_org_flag_override_audit(
            session,
            current_user,
            flag_name=flag_name,
            enabled=req.enabled,
        )
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
    if current_user.organisation_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=_MSG_ORG_ID_REQUIRED,
        )
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
        await _emit_org_flag_override_audit(
            session,
            current_user,
            flag_name=flag_name,
            enabled=None,
        )
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
