"""Plugin listing and health-check REST API.

Returns read-only metadata about installed Modulo plugins discovered at startup.
Plugin management (install, uninstall, upgrade) is done via pip — not through this API.
"""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.core.plugin_registry import PluginHealth, PluginManifest, get_plugin_registry

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


@router.get("", response_model=list[PluginResponse])
async def list_plugins_endpoint(
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> list[PluginResponse]:
    registry = get_plugin_registry()
    health_results = registry.health_check()
    return [
        _to_response(manifest, health_results.get(pid, PluginHealth(ok=False, detail="Unknown")))
        for pid, manifest in registry.list_plugins().items()
    ]


@router.get("/{plugin_id}/health")
async def plugin_health_endpoint(
    plugin_id: str,
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> PluginHealth:
    registry = get_plugin_registry()
    manifest = registry.get_plugin(plugin_id)
    if manifest is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plugin not found")
    return registry.health_check(plugin_id)[plugin_id]
