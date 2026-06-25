"""Minimal /api/v1/me endpoint — delegates to auth's /me logic."""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.dependencies import get_db_session
from modulo.api.routes.auth import MeResponse
from modulo.api.routes.auth import me as _me_handler
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.db.crud.user import get_user_by_id
from modulo.db.models.user import User

router = APIRouter(prefix="/api/v1", tags=["user"])


@router.get("/me", response_model=MeResponse)
async def current_user_profile(
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> MeResponse:
    return await _me_handler(current_user, session)


class SettingsResponse(BaseModel):
    theme: str | None = None


class SettingsUpdate(BaseModel):
    theme: str | None = None


@router.get("/me/settings", response_model=SettingsResponse)
async def get_user_settings(
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    user = await get_user_by_id(session, current_user.user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return user.preferences


@router.put("/me/settings")
async def update_user_settings(
    body: SettingsUpdate,
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    user = await get_user_by_id(session, current_user.user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    prefs = dict(user.preferences or {})
    if body.theme is not None:
        prefs["theme"] = body.theme
    user.preferences = prefs
    session.add(user)
    await session.commit()
    return user.preferences
