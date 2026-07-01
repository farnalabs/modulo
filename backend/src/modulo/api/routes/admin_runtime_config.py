"""Admin runtime-config introspection and overrides."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status

from modulo.api.middleware.sensitive_mask import is_sensitive_env_key, mask_sensitive_value
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.core.runtime_config.store import get_runtime_config_store

router = APIRouter(prefix="/api/v1/admin/runtime-config", tags=["admin-runtime-config"])


def _require_admin(principal: AuthenticatedPrincipal) -> None:
    if principal.org_role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admin users can manage runtime config",
        )


def _mask_sensitive_items(items: list[dict[str, Any]]) -> None:
    for item in items:
        if is_sensitive_env_key(item["key"]):
            for field in ("current_value", "default_value", "env_value", "override_value"):
                if isinstance(item.get(field), str):
                    item[field] = mask_sensitive_value(item[field])


def _calc_has_drift(items: list[dict[str, Any]]) -> bool:
    return any(
        item["env_value"] is not None
        and item["override_value"] is None
        and item["current_value"] != item["env_value"]
        for item in items
    )


@router.get("")
async def get_runtime_config(
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
) -> dict[str, Any]:
    _require_admin(current_user)
    store = get_runtime_config_store()
    items = [asdict(item) for item in store.get_all()]
    _mask_sensitive_items(items)
    return {
        "items": items,
        "has_drift": _calc_has_drift(items),
    }


@router.put("")
async def set_runtime_config_overrides(
    body: dict[str, Any],
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
) -> dict[str, Any]:
    _require_admin(current_user)
    store = get_runtime_config_store()
    overrides = body.get("overrides", {})
    if not isinstance(overrides, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="'overrides' must be a dict",
        )
    for key, value in overrides.items():
        store.set_override(key, str(value))

    clear_keys = body.get("clear", [])
    if not isinstance(clear_keys, list):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="'clear' must be a list",
        )
    for key in clear_keys:
        store.clear_override(key)

    items = [asdict(item) for item in store.get_all()]
    _mask_sensitive_items(items)
    return {
        "items": items,
        "has_drift": _calc_has_drift(items),
    }


@router.post("/reload")
async def reload_runtime_config(
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
) -> dict[str, Any]:
    _require_admin(current_user)
    store = get_runtime_config_store()
    store.reload()
    items = [asdict(item) for item in store.get_all()]
    _mask_sensitive_items(items)
    return {
        "items": items,
        "has_drift": _calc_has_drift(items),
    }
