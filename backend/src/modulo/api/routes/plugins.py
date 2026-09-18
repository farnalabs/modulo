"""Plugin listing and health-check REST API.

Returns read-only metadata about installed Modulo plugins discovered at startup.
Plugin management (install, uninstall, upgrade) is done via pip — not through this API.
"""

import logging
from datetime import datetime

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from modulo.api.dependencies import require_feature, require_permission
from modulo.auth.jwt import TenantPrincipal
from modulo.core.plugin_registry import PluginHealth, PluginManifest, get_plugin_registry
from modulo.util import sanitise_log_value as _sanitise_log_value

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/plugins", tags=["plugins"])


class PluginResponse(BaseModel):
    """API representation of an installed plugin with health status."""

    PLUGIN_ID: str
    display_name: str
    description: str
    version: str
    capabilities: set[str]
    health_ok: bool
    health_detail: str = ""
    health_checked_at: datetime | None = None

    model_config = {"from_attributes": False}


def _to_response(manifest: PluginManifest, health: PluginHealth) -> PluginResponse:
    return PluginResponse(
        PLUGIN_ID=manifest.PLUGIN_ID,
        display_name=manifest.display_name,
        description=manifest.description,
        version=manifest.version,
        capabilities=manifest.capabilities,
        health_ok=health.ok,
        health_detail=health.detail,
        health_checked_at=health.checked_at,
    )


@router.get("", dependencies=[require_feature("plugin_management")])
async def list_plugins_endpoint(
    _principal: TenantPrincipal = require_permission("plugin.list"),
) -> list[PluginResponse]:
    try:
        registry = get_plugin_registry()
        health_results = registry.health_check()
        return [
            _to_response(manifest, health_results.get(pid, PluginHealth(ok=False, detail="Unknown")))
            for pid, manifest in registry.list_plugins().items()
        ]
    except Exception:
        logger.exception("Failed to list plugins")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to list plugins",
        ) from None


@router.get("/{plugin_id}/health", dependencies=[require_feature("plugin_management")])
async def plugin_health_endpoint(
    plugin_id: str,
    _principal: TenantPrincipal = require_permission("plugin.list"),
) -> PluginHealth:
    try:
        registry = get_plugin_registry()
        manifest = registry.get_plugin(plugin_id)
        if manifest is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plugin not found")
        return registry.health_check(plugin_id)[plugin_id]
    except HTTPException:
        raise
    except Exception:
        logger.exception("Failed to check plugin health for %s", _sanitise_log_value(plugin_id))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to check plugin health",
        ) from None
