import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, field_validator, model_validator
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.constants import MSG_INTERNAL_SERVER_ERROR
from modulo.api.db_error_handling import handle_db_errors
from modulo.api.dependencies import get_db_session, require_feature, require_system_permission
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.db.crud.system_config import get_config, update_config

# WARNING: system.config.manage is currently ONLY assignable to is_system_admin
# users. There is NO in-product path to grant is_system_admin. Self-hosted
# single-org deployments that lock themselves out will need manual DB
# intervention to recover. A follow-up design decision is needed for an
# emergency-recovery path before this permission can be safely broadened.

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

# Required per-backend field (canonical API keys as written by the frontend) when
# the backend is enabled. Mirrors the field schemas in `frontend/src/monitor/types.ts`.
_REQUIRED_BACKEND_FIELDS: dict[str, str] = {
    "sentry": "dsn",
    "datadog_rum": "clientToken",
    "grafana_faro": "url",
}


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
    @model_validator(mode="after")
    def validate_per_backend_fields(self) -> "MonitorConfigUpdate":
        for backend in self.backends:
            required_field = _REQUIRED_BACKEND_FIELDS.get(backend)
            if required_field is None:
                continue
            config = getattr(self, backend, None)
            if config is None or not config.get(required_field):
                raise ValueError(f"Backend '{backend}' is enabled but missing required field '{required_field}'")
        return self


def _merge(entry: Any | None) -> dict[str, Any]:
    if entry is None:
        return dict(DEFAULT_CONFIG)
    value = entry.value
    if value is None or not isinstance(value, dict):
        return dict(DEFAULT_CONFIG)
    return {**DEFAULT_CONFIG, **value}


@router.get("", response_model=MonitorConfigResponse, dependencies=[require_feature("error_tracking")])
@handle_db_errors("admin.monitor_config.get_monitor_config")
async def get_monitor_config(
    session: Annotated[AsyncSession, Depends(get_db_session)],
    current_user: AuthenticatedPrincipal = require_system_permission("system.config.manage"),  # type: ignore[assignment]
) -> dict[str, Any]:
    try:
        async with session.begin():
            entry = await get_config(session, _CONFIG_KEY)
    except ProgrammingError:
        _log.exception("admin.monitor_config.get_monitor_config - table missing")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    except SQLAlchemyError:
        _log.exception("admin.monitor_config.get_monitor_config - SQL error")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database temporarily unavailable — this may be a transient issue. Try refreshing the page.",
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception("Unexpected error in get_monitor_config")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None
    return _merge(entry)


@router.put("", response_model=MonitorConfigResponse, dependencies=[require_feature("error_tracking")])
@handle_db_errors("admin.monitor_config.set_monitor_config")
async def set_monitor_config(
    req: MonitorConfigUpdate,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    current_user: AuthenticatedPrincipal = require_system_permission("system.config.manage"),  # type: ignore[assignment]
) -> dict[str, Any]:
    try:
        async with session.begin():
            entry = await update_config(
                session,
                _CONFIG_KEY,
                req.model_dump(),
                updated_by=current_user.account_id,
            )
    except ProgrammingError:
        _log.exception("admin.monitor_config.set_monitor_config - table missing")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    except SQLAlchemyError:
        _log.exception("admin.monitor_config.set_monitor_config - SQL error")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database temporarily unavailable — this may be a transient issue. Try refreshing the page.",
        ) from None
    except HTTPException:
        raise
    except Exception:
        _log.exception("Unexpected error in set_monitor_config")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None
    return _merge(entry)
