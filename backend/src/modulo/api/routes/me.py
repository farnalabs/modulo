"""Minimal /api/v1/me endpoint — delegates to auth's /me logic."""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.dependencies import get_db_session
from modulo.api.routes.auth import MeResponse
from modulo.api.routes.auth import me as _me_handler
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.auth.passwords import hash_password, validate_password_strength
from modulo.db.crud.account import get_account_by_id, update_account_preferences
from modulo.db.crud.token_family import blacklist_family, list_families_for_account

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
    account = await get_account_by_id(session, current_user.account_id)
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found")
    return account.preferences


@router.put("/me/settings")
async def update_user_settings(
    body: SettingsUpdate,
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    prefs = {}
    if body.theme is not None:
        prefs["theme"] = body.theme
    return await update_account_preferences(session, current_user.account_id, prefs)


class PasswordChangeRequest(BaseModel):
    current_password: str = Field(min_length=1)
    new_password: str = Field(min_length=8)


@router.put("/me/password", status_code=status.HTTP_200_OK)
async def change_password(
    body: PasswordChangeRequest,
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, str]:
    from modulo.auth.passwords import verify_password

    async with session.begin():
        account = await get_account_by_id(session, current_user.account_id)
        if account is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found")

        if not account.password_hash or not verify_password(body.current_password, account.password_hash):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Current password is incorrect")

        try:
            validate_password_strength(body.new_password)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc

        account.password_hash = hash_password(body.new_password)
        session.add(account)

        families = await list_families_for_account(session, current_user.account_id)
        for family in families:
            await blacklist_family(session, family.family_id)

    return {"detail": "Password changed successfully"}
