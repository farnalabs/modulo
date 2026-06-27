"""Admin feature flag inspection — lists all known flags and their current status."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status

from modulo.core.feature_flags import FeatureFlagRegistry
from modulo.settings import Settings, get_settings

router = APIRouter(prefix="/api/v1/admin/feature-flags", tags=["admin-feature-flags"])


def _build_registry(settings: Settings) -> FeatureFlagRegistry:
    has_key = bool(settings.modulo_license_key)
    tier = "enterprise" if has_key else "free"
    return FeatureFlagRegistry(current_tier=tier, has_license_key=has_key)


@router.get("")
async def list_feature_flags(
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    registry = _build_registry(settings)
    has_key = bool(settings.modulo_license_key)
    tier = "enterprise" if has_key else "free"
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


@router.get("/{flag_name}")
async def get_feature_flag(
    flag_name: str,
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    registry = _build_registry(settings)
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
