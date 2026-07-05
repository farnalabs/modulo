"""Admin runtime-config introspection and overrides."""

from __future__ import annotations

import os
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status

from modulo.api.middleware.sensitive_mask import is_sensitive_env_key, mask_sensitive_value
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.core.runtime_config.store import KNOWN_KEYS, RuntimeConfigStore, get_runtime_config_store

router = APIRouter(prefix="/api/v1/admin/runtime-config", tags=["admin-runtime-config"])


def _require_admin(principal: AuthenticatedPrincipal) -> None:
    if principal.org_role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Only admin users can manage runtime config (your role: {principal.org_role})",
        )


def _mask_sensitive_items(items: list[dict[str, Any]]) -> None:
    for item in items:
        if is_sensitive_env_key(item["key"]):
            for field in ("current_value", "default_value", "env_value", "override_value"):
                if isinstance(item.get(field), str):
                    item[field] = mask_sensitive_value(item[field])


def _calc_has_drift(items: list[dict[str, Any]]) -> bool:
    return any(item["override_value"] is None and os.environ.get(item["key"]) != item["env_value"] for item in items)


def _validate_known_key(key: str, context: str) -> None:
    if key not in KNOWN_KEYS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown config key in {context}: {key}",
        )


def _build_response(store: RuntimeConfigStore) -> dict[str, Any]:
    items = [asdict(item) for item in store.get_all()]
    has_drift = _calc_has_drift(items)
    _mask_sensitive_items(items)
    return {"items": items, "has_drift": has_drift}


@router.get("")
def get_runtime_config(
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
) -> dict[str, Any]:
    _require_admin(current_user)
    return _build_response(get_runtime_config_store())


@router.put("")
def set_runtime_config_overrides(
    req: dict[str, Any],
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
) -> dict[str, Any]:
    _require_admin(current_user)
    store = get_runtime_config_store()

    overrides = req.get("overrides", {})
    if not isinstance(overrides, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="'overrides' must be a dict",
        )
    validated_overrides: list[tuple[str, str]] = []
    for key, value in overrides.items():
        _validate_known_key(key, "override")
        if not isinstance(value, str):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Override value for '{key}' must be a string, got {type(value).__name__}",
            )
        validated_overrides.append((key, value))

    clear_keys = req.get("clear", [])
    if not isinstance(clear_keys, list):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="'clear' must be a list",
        )
    validated_clear: list[str] = []
    for key in clear_keys:
        if not isinstance(key, str):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Clear key must be a string, got {type(key).__name__}",
            )
        _validate_known_key(key, "clear")
        validated_clear.append(key)

    for key, value in validated_overrides:
        store.set_override(key, value)
    for key in validated_clear:
        store.clear_override(key)

    return _build_response(store)


@router.post("/reload")
def reload_runtime_config(
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
) -> dict[str, Any]:
    _require_admin(current_user)
    store = get_runtime_config_store()
    store.reload()
    return _build_response(store)
