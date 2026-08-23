"""Admin-only routes for deployment-wide SystemConfig management."""

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.constants import MSG_INTERNAL_SERVER_ERROR
from modulo.api.db_error_handling import handle_db_errors
from modulo.api.dependencies import get_db_session, require_system_permission
from modulo.api.middleware.sensitive_mask import is_sensitive_key, mask_sensitive_value
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.db.crud.system_config import delete_config, list_config, update_config

_CODE_SYSTEM_CONFIG_MANAGE = "system.config.manage"
_MSG_DATABASE_NOT_AVAILABLE_RUN = "Database not available. Run migrations."
_CODE_ROUTES_ADMIN_SYSTEM_CONFIG = "routes.admin_system_config"
_MSG_DATABASE_ERROR_OCCURRED_PLEASE = "A database error occurred. Please try again later."


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/system-admin/config", tags=["admin-system-config"])


class ConfigEntry(BaseModel):
    key: str
    value: Any
    updated_at: str | None = None


@router.get(
    "",
    responses={
        500: {"description": "Internal Server Error"},
        501: {"description": "Not Implemented"},
        503: {"description": "Service Unavailable"},
    },
)
@handle_db_errors("admin.system_config.admin_list_config")
async def admin_list_config(
    current_user: AuthenticatedPrincipal = require_system_permission(_CODE_SYSTEM_CONFIG_MANAGE),  # type: ignore[assignment]
    session: AsyncSession = Depends(get_db_session),
) -> list[ConfigEntry]:
    try:
        async with session.begin():
            entries = await list_config(session)
        return [
            ConfigEntry(
                key=e.key,
                value=(
                    mask_sensitive_value(e.value) if isinstance(e.value, str) and is_sensitive_key(e.key) else e.value
                ),
                updated_at=e.updated_at.isoformat() if e.updated_at else None,
            )
            for e in entries
        ]
    except HTTPException:
        raise
    except asyncio.CancelledError:
        raise
    except ProgrammingError:
        logger.exception("admin_system_config.admin_list_config")
        raise HTTPException(status_code=501, detail=_MSG_DATABASE_NOT_AVAILABLE_RUN) from None
    except SQLAlchemyError:
        logger.exception(_CODE_ROUTES_ADMIN_SYSTEM_CONFIG)

        raise HTTPException(
            status_code=503,
            detail=_MSG_DATABASE_ERROR_OCCURRED_PLEASE,
        ) from None
    except Exception:
        logger.exception("Unexpected error in admin_list_config")
        raise HTTPException(status_code=500, detail=MSG_INTERNAL_SERVER_ERROR) from None


class SetConfigRequest(BaseModel):
    value: Any = Field(..., description="JSON value to store")


@router.put(
    "/{key}",
    responses={
        409: {"description": "Conflict"},
        500: {"description": "Internal Server Error"},
        501: {"description": "Not Implemented"},
        503: {"description": "Service Unavailable"},
    },
)
@handle_db_errors("admin.system_config.admin_set_config")
async def admin_set_config(
    key: str,
    req: SetConfigRequest,
    current_user: AuthenticatedPrincipal = require_system_permission(_CODE_SYSTEM_CONFIG_MANAGE),  # type: ignore[assignment]
    session: AsyncSession = Depends(get_db_session),
) -> ConfigEntry:
    try:
        async with session.begin():
            entry = await update_config(session, key, req.value, updated_by=current_user.account_id)
        return ConfigEntry(
            key=entry.key,
            value=entry.value,
            updated_at=entry.updated_at.isoformat() if entry.updated_at else None,
        )
    except HTTPException:
        raise
    except asyncio.CancelledError:
        raise
    except IntegrityError:
        logger.exception("admin_system_config.admin_set_config")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Resource already exists or constraint violation.",
        ) from None
    except ProgrammingError:
        logger.exception("admin_system_config.admin_set_config")
        raise HTTPException(status_code=501, detail=_MSG_DATABASE_NOT_AVAILABLE_RUN) from None
    except SQLAlchemyError:
        logger.exception(_CODE_ROUTES_ADMIN_SYSTEM_CONFIG)

        raise HTTPException(
            status_code=503,
            detail=_MSG_DATABASE_ERROR_OCCURRED_PLEASE,
        ) from None
    except Exception:
        logger.exception("Unexpected error in admin_set_config")
        raise HTTPException(status_code=500, detail=MSG_INTERNAL_SERVER_ERROR) from None


@router.delete(
    "/{key}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        404: {"description": "Not Found"},
        500: {"description": "Internal Server Error"},
        501: {"description": "Not Implemented"},
        503: {"description": "Service Unavailable"},
    },
)
@handle_db_errors("admin.system_config.admin_delete_config")
async def admin_delete_config(
    key: str,
    current_user: AuthenticatedPrincipal = require_system_permission(_CODE_SYSTEM_CONFIG_MANAGE),  # type: ignore[assignment]
    session: AsyncSession = Depends(get_db_session),
) -> None:
    try:
        async with session.begin():
            deleted = await delete_config(session, key)
        if not deleted:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Config key '{key}' not found",
            )
    except HTTPException:
        raise
    except asyncio.CancelledError:
        raise
    except ProgrammingError:
        logger.exception("admin_system_config.admin_delete_config")
        raise HTTPException(status_code=501, detail=_MSG_DATABASE_NOT_AVAILABLE_RUN) from None
    except SQLAlchemyError:
        logger.exception(_CODE_ROUTES_ADMIN_SYSTEM_CONFIG)

        raise HTTPException(
            status_code=503,
            detail=_MSG_DATABASE_ERROR_OCCURRED_PLEASE,
        ) from None
    except Exception:
        logger.exception("Unexpected error in admin_delete_config")
        raise HTTPException(status_code=500, detail=MSG_INTERNAL_SERVER_ERROR) from None
