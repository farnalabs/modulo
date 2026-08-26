"""Admin dev-mode toggle — enables preview/in-development features."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.dependencies import get_db_session, require_system_permission
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.db.crud.system_config import get_config, update_config
from modulo.settings import Settings, get_settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/admin/dev-mode", tags=["admin-dev-mode"])


class DevModeResponse(BaseModel):
    enabled: bool
    source: str  # "env" | "db" | "default"


class SetDevModeRequest(BaseModel):
    enabled: bool


@router.get("", response_model=DevModeResponse)
async def get_dev_mode(
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_db_session),
    _: AuthenticatedPrincipal = Depends(get_current_user),
) -> dict[str, Any]:
    """Get current dev mode status.

    Resolution: SystemConfig override → env var → false.
    """
    # 1. Check DB override
    try:
        async with session.begin():
            config = await get_config(session, "dev_mode")
        if config is not None:
            return {"enabled": bool(config.value), "source": "db"}
    except Exception:
        logger.warning("Failed to read dev_mode from DB", exc_info=True)

    # 2. Check env var
    if settings.modulo_dev_mode:
        return {"enabled": True, "source": "env"}

    # 3. Default
    return {"enabled": False, "source": "default"}


@router.put("", response_model=DevModeResponse, responses={500: {"description": "Internal Server Error"}})
async def set_dev_mode(
    req: SetDevModeRequest,
    _settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_db_session),
    _: AuthenticatedPrincipal = require_system_permission("system.config.manage"),  # type: ignore[assignment]
) -> dict[str, Any]:
    """Enable or disable dev mode. Persisted in SystemConfig."""
    try:
        async with session.begin():
            await update_config(session, "dev_mode", req.enabled)
        return {"enabled": req.enabled, "source": "db"}
    except Exception:
        logger.exception("Failed to set dev_mode")
        raise HTTPException(status_code=500, detail="Failed to set dev mode") from None
