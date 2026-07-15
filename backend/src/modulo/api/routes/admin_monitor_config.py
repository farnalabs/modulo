import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, field_validator
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.db_error_handling import handle_db_errors
from modulo.api.dependencies import get_db_session
from modulo.auth.dependencies import get_current_tenant_user
from modulo.auth.jwt import TenantPrincipal
from modulo.db.crud.system_config import get_config, set_config

logger = logging.getLogger(__name__)

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/admin/monitor-config", tags=["admin-monitor-config"])

_CONFIG_KEY = "monitor_backends"

DEFAULT_CONFIG: dict[str, Any] = {
    "backends": ["builtin"],
    "sentry": None,
    "datadog_rum": None,
    "grafana_faro": None,
}

_KNOWN_BACKENDS = frozenset({"builtin", "sentry", "datadog_rum", "grafana_faro"})


def _require_admin(principal: TenantPrincipal) -> None:
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

    @field_validator("backends")
    @classmethod
    def validate_backend_names(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("At least one backend must be specified")
        unknown = [b for b in v if b not in _KNOWN_BACKENDS]
        if unknown:
            raise ValueError(f"Unknown backend(s): {', '.join(unknown)}. Known: {', '.join(sorted(_KNOWN_BACKENDS))}")
        return v


class MonitorConfigResponse(MonitorConfigBase):
    pass


class MonitorConfigUpdate(MonitorConfigBase):
    pass


def _merge(entry: Any | None) -> dict[str, Any]:
    if entry is None:
        return dict(DEFAULT_CONFIG)
    value = entry.value
    if value is None or not isinstance(value, dict):
        return dict(DEFAULT_CONFIG)
    return {**DEFAULT_CONFIG, **value}


@router.get("", response_model=MonitorConfigResponse)
@handle_db_errors("admin.monitor_config.get_monitor_config")
@router.get("", response_model=MonitorConfigResponse)
async def get_monitor_config(
    current_user: TenantPrincipal = Depends(get_current_tenant_user),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    _require_admin(current_user)
    try:
        entry = await get_config(session, _CONFIG_KEY)
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    except SQLAlchemyError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database error while fetching monitor config.",
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception("Unexpected error in get_monitor_config")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        ) from None
    return _merge(entry)


@router.put("", response_model=MonitorConfigResponse)
@handle_db_errors("admin.monitor_config.set_monitor_config")
@router.put("", response_model=MonitorConfigResponse)
async def set_monitor_config(
    req: MonitorConfigUpdate,
    current_user: TenantPrincipal = Depends(get_current_tenant_user),
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
        ) from None
    except SQLAlchemyError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database error while setting monitor config.",
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception("Unexpected error in set_monitor_config")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        ) from None
    return _merge(entry)
