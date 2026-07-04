"""Admin-only routes for deployment-wide SystemConfig management."""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.dependencies import get_db_session
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.db.crud.system_config import delete_config, list_config, set_config

router = APIRouter(prefix="/api/v1/system-admin/config", tags=["admin-system-config"])


class ConfigEntry(BaseModel):
    key: str
    value: Any
    updated_at: str | None = None


@router.get("")
async def admin_list_config(
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> list[ConfigEntry]:
    if not current_user.is_system_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="System admin role required",
        )
    entries = await list_config(session)
    return [
        ConfigEntry(
            key=e.key,
            value=e.value,
            updated_at=e.updated_at.isoformat() if e.updated_at else None,
        )
        for e in entries
    ]


class SetConfigRequest(BaseModel):
    value: Any = Field(..., description="JSON value to store")


@router.put("/{key}")
async def admin_set_config(
    key: str,
    req: SetConfigRequest,
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> ConfigEntry:
    if not current_user.is_system_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="System admin role required",
        )
    entry = await set_config(session, key, req.value, updated_by=current_user.account_id)
    return ConfigEntry(
        key=entry.key,
        value=entry.value,
        updated_at=entry.updated_at.isoformat() if entry.updated_at else None,
    )


@router.delete("/{key}", status_code=status.HTTP_204_NO_CONTENT)
async def admin_delete_config(
    key: str,
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> None:
    if not current_user.is_system_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="System admin role required",
        )
    deleted = await delete_config(session, key)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Config key '{key}' not found",
        )
