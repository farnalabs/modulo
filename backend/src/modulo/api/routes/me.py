"""Minimal /api/v1/me endpoint — delegates to auth's /me logic."""

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.dependencies import get_db_session
from modulo.api.routes.admin_remy import (
    SkillCreate,
    SkillResponse,
    SkillUpdate,
    _skill_to_response,
    get_user_skill_or_404,
    get_user_skills,
)
from modulo.api.routes.auth import MeResponse
from modulo.api.routes.auth import me as _me_handler
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.auth.passwords import hash_password, validate_password_strength
from modulo.db.crud.account import get_account_by_id, update_account_preferences
from modulo.db.crud.token_family import blacklist_family, list_families_for_account
from modulo.db.models.remy_skill import RemySkill

router = APIRouter(prefix="/api/v1", tags=["user"])


@router.get("/me", response_model=MeResponse)
async def current_user_profile(
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> MeResponse:
    return await _me_handler(current_user, session)


class SettingsResponse(BaseModel):
    theme: str | None = None
    locale: str | None = None


class SettingsUpdate(BaseModel):
    theme: str | None = None
    locale: str | None = None


@router.get("/me/settings", response_model=SettingsResponse)
async def get_user_settings(
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    try:
        account = await get_account_by_id(session, current_user.account_id)
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found")
    return account.preferences


SUPPORTED_LOCALES = {"en-US"}


@router.put("/me/settings")
async def update_user_settings(
    body: SettingsUpdate,
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    prefs = {}
    if body.theme is not None:
        prefs["theme"] = body.theme
    if body.locale is not None:
        prefs["locale"] = body.locale
    try:
        return await update_account_preferences(session, current_user.account_id, prefs)
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None


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


# ── User-level Remy Skills ────────────────────────────────────────────


@router.get("/me/remy/skills", response_model=list[SkillResponse])
async def list_user_skills(
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> list[SkillResponse]:
    try:
        async with session.begin():
            skills = await get_user_skills(session, current_user.account_id)
        return [_skill_to_response(s) for s in skills]
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None


@router.post("/me/remy/skills", response_model=SkillResponse, status_code=status.HTTP_201_CREATED)
async def create_user_skill(
    body: SkillCreate,
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> SkillResponse:
    try:
        async with session.begin():
            skill = RemySkill(
                id=uuid.uuid4(),
                organisation_id=None,
                user_id=current_user.account_id,
                name=body.name,
                description=body.description,
                triggers=body.triggers,
                body=body.body,
                active=body.active,
            )
            session.add(skill)
            await session.flush()
        return _skill_to_response(skill)
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None


@router.put("/me/remy/skills/{skill_id}", response_model=SkillResponse)
async def update_user_skill(
    skill_id: uuid.UUID,
    body: SkillUpdate,
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> SkillResponse:
    try:
        async with session.begin():
            skill = await get_user_skill_or_404(session, current_user.account_id, skill_id)
            if body.name is not None:
                skill.name = body.name
            if body.description is not None:
                skill.description = body.description
            if body.triggers is not None:
                skill.triggers = body.triggers
            if body.body is not None:
                skill.body = body.body
            if body.active is not None:
                skill.active = body.active
            await session.flush()
        return _skill_to_response(skill)
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None


@router.delete("/me/remy/skills/{skill_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user_skill(
    skill_id: uuid.UUID,
    current_user: AuthenticatedPrincipal = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> None:
    try:
        async with session.begin():
            skill = await get_user_skill_or_404(session, current_user.account_id, skill_id)
            await session.delete(skill)
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None
