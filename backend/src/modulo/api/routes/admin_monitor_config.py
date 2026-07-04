from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import ProgrammingError

from modulo.api.dependencies import get_db_session
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.db.crud.system_config import get_config, set_config

router = APIRouter(prefix="/api/v1/admin/monitor-config", tags=["admin-monitor-config"])

_CONFIG_KEY = "monitor_backends"

DEFAULT_CONFIG: dict[str, Any] = {
    "backends": ["builtin"],
    "sentry": None,
    "datadog_rum": None,
    "grafana_faro": None,
}


def _require_admin(principal: AuthenticatedPrincipal) -> None:
    if principal.org_role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin role required",
        )


class MonitorConfigBase(BaseModel):
    backends: list[str]
    sentry: dict[str, Any] | None = None
    datadog_rum: dict[str, Any] | None = None
    grafana_faro: dict[str, Any] | None = None


class MonitorConfigResponse(MonitorConfigBase):
    pass


class MonitorConfigUpdate(MonitorConfigBase):
    pass


def _merge(entry: Any | None) -> dict[str, Any]:
    if entry is None or entry.value is None:
        return dict(DEFAULT_CONFIG)
    return {**DEFAULT_CONFIG, **entry.value}


@router.get("", response_model=MonitorConfigResponse)
async def get_monitor_config(
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    _require_admin(current_user)
    try:
        entry = await get_config(session, _CONFIG_KEY)
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        )
    return _merge(entry)


@router.put("", response_model=MonitorConfigResponse)
async def set_monitor_config(
    req: MonitorConfigUpdate,
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    _require_admin(current_user)
    try:
        entry = await set_config(
            session,
            _CONFIG_KEY,
            req.model_dump(),
            updated_by=current_user.account_id,
        )
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        )
    return _merge(entry)
