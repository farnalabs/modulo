"""Minimal /api/v1/me endpoint — delegates to auth's /me logic."""

import asyncio
import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.constants import MSG_THIS_FEATURE_NOT_AVAILABLE
from modulo.api.db_error_handling import handle_db_errors
from modulo.api.dependencies import get_db_session
from modulo.api.routes.admin_remy import (
    SkillCreate,
    SkillResponse,
    SkillUpdate,
    _skill_to_response,
    get_user_skill_or_404,
    get_user_skills,
)
from modulo.auth.dependencies import get_current_tenant_user
from modulo.auth.jwt import TenantPrincipal
from modulo.auth.passwords import hash_password, validate_password_strength, verify_password
from modulo.core.remy.context_source_service import (
    ContextSourceResponseItem,
    RemyContextSourceService,
)
from modulo.db.crud.account import get_account_by_id, update_account_preferences
from modulo.db.crud.token_family import blacklist_family, list_families_for_account
from modulo.db.models.remy_skill import RemySkill
from modulo.db.rls import set_rls_org

_CODE_ROUTES_ME = "routes.me"
_MSG_ACCOUNT_NOT_FOUND = "Account not found"


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["user"])


class SettingsResponse(BaseModel):
    theme: str | None = None
    locale: str | None = None


class SettingsUpdate(BaseModel):
    theme: str | None = None
    locale: str | None = None


@router.get("/me/settings", response_model=SettingsResponse)
@handle_db_errors("me.get_user_settings")
async def get_user_settings(
    current_user: TenantPrincipal = Depends(get_current_tenant_user),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    try:
        async with session.begin():
            account = await get_account_by_id(session, current_user.account_id)
    except ProgrammingError:
        logger.exception(_CODE_ROUTES_ME)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_THIS_FEATURE_NOT_AVAILABLE,
        ) from None

    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_ACCOUNT_NOT_FOUND)
    return account.preferences


@router.put("/me/settings", response_model=SettingsResponse)
@handle_db_errors("me.update_user_settings")
async def update_user_settings(
    req: SettingsUpdate | None = None,
    current_user: TenantPrincipal = Depends(get_current_tenant_user),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    if req is None:
        try:
            async with session.begin():
                account = await get_account_by_id(session, current_user.account_id)
        except ProgrammingError:
            logger.exception(_CODE_ROUTES_ME)
            raise HTTPException(
                status_code=status.HTTP_501_NOT_IMPLEMENTED,
                detail=MSG_THIS_FEATURE_NOT_AVAILABLE,
            ) from None

        if account is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_ACCOUNT_NOT_FOUND)
        return account.preferences
    prefs: dict[str, object] = {}
    if req.theme is not None:
        prefs["theme"] = req.theme
    if req.locale is not None:
        prefs["locale"] = req.locale
    try:
        async with session.begin():
            return await update_account_preferences(session, current_user.account_id, prefs)
    except ProgrammingError:
        logger.exception(_CODE_ROUTES_ME)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_THIS_FEATURE_NOT_AVAILABLE,
        ) from None


class PasswordChangeRequest(BaseModel):
    current_password: str = Field(min_length=1)
    new_password: str = Field(min_length=8)


@router.put("/me/password", status_code=status.HTTP_200_OK)
@handle_db_errors("me.change_password")
async def change_password(
    req: PasswordChangeRequest,
    current_user: TenantPrincipal = Depends(get_current_tenant_user),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, str]:
    try:
        async with session.begin():
            account = await get_account_by_id(session, current_user.account_id)
            if account is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_MSG_ACCOUNT_NOT_FOUND)

            if not account.password_hash or not verify_password(req.current_password, account.password_hash):
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Current password is incorrect")

            if req.new_password == req.current_password:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="New password must be different from the current password",
                )

            try:
                validate_password_strength(req.new_password)
            except ValueError as exc:
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

            account.password_hash = hash_password(req.new_password)
            session.add(account)

            families = await list_families_for_account(session, current_user.account_id)
            for family in families:
                try:
                    await blacklist_family(session, family.family_id, current_user.account_id)
                except HTTPException:
                    raise
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("me.change_password")
                    logger.warning(
                        "Failed to blacklist previous token family during password change for account %s",
                        current_user.account_id,
                    )

            # Audit is fail-open-with-alert: the password change ALWAYS commits;
            # a failed audit write is loudly logged and never rolls back the change.
            try:
                from modulo.core.audit_logger import append_audit_event

                await set_rls_org(session, current_user.organisation_id)
                await append_audit_event(
                    session,
                    org_id=current_user.organisation_id,
                    event_type="password_changed",
                    actor_user_id=current_user.account_id,
                    resource_type="account",
                    resource_id=current_user.account_id,
                    payload_json={"method": "self_service"},
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("me.change_password audit write failed")

    except ProgrammingError:
        logger.exception(_CODE_ROUTES_ME)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_THIS_FEATURE_NOT_AVAILABLE,
        ) from None

    return {"detail": "Password changed successfully"}


# ── User-level Remy Skills ────────────────────────────────────────────


@router.get("/me/remy/skills")
@handle_db_errors("me.list_user_skills")
async def list_user_skills(
    current_user: TenantPrincipal = Depends(get_current_tenant_user),
    session: AsyncSession = Depends(get_db_session),
) -> list[SkillResponse]:
    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            skills = await get_user_skills(session, current_user.account_id, current_user.organisation_id)
    except ProgrammingError:
        logger.exception(_CODE_ROUTES_ME)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_THIS_FEATURE_NOT_AVAILABLE,
        ) from None

    return [_skill_to_response(s) for s in skills]


@router.post("/me/remy/skills", status_code=status.HTTP_201_CREATED)
@handle_db_errors("me.create_user_skill")
async def create_user_skill(
    req: SkillCreate,
    current_user: TenantPrincipal = Depends(get_current_tenant_user),
    session: AsyncSession = Depends(get_db_session),
) -> SkillResponse:
    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            skill = RemySkill(
                id=uuid.uuid4(),
                organisation_id=None,
                user_id=current_user.account_id,
                name=req.name,
                description=req.description,
                triggers=req.triggers,
                body=req.body,
                active=req.active,
            )
            session.add(skill)
            await session.flush()
    except ProgrammingError:
        logger.exception(_CODE_ROUTES_ME)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_THIS_FEATURE_NOT_AVAILABLE,
        ) from None

    return _skill_to_response(skill)


@router.put("/me/remy/skills/{skill_id}")
@handle_db_errors("me.update_user_skill")
async def update_user_skill(
    skill_id: uuid.UUID,
    req: SkillUpdate,
    current_user: TenantPrincipal = Depends(get_current_tenant_user),
    session: AsyncSession = Depends(get_db_session),
) -> SkillResponse:
    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            skill = await get_user_skill_or_404(session, current_user.account_id, skill_id)
            if req.name is not None:
                skill.name = req.name
            if req.description is not None:
                skill.description = req.description
            if req.triggers is not None:
                skill.triggers = req.triggers
            if req.body is not None:
                skill.body = req.body
            if req.active is not None:
                skill.active = req.active
            await session.flush()
    except ProgrammingError:
        logger.exception(_CODE_ROUTES_ME)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_THIS_FEATURE_NOT_AVAILABLE,
        ) from None

    return _skill_to_response(skill)


@router.delete("/me/remy/skills/{skill_id}", status_code=status.HTTP_204_NO_CONTENT)
@handle_db_errors("me.delete_user_skill")
async def delete_user_skill(
    skill_id: uuid.UUID,
    current_user: TenantPrincipal = Depends(get_current_tenant_user),
    session: AsyncSession = Depends(get_db_session),
) -> None:
    try:
        async with session.begin():
            await set_rls_org(session, current_user.organisation_id)
            skill = await get_user_skill_or_404(session, current_user.account_id, skill_id)
            await session.delete(skill)

    # ── User-level Context Sources ─────────────────────────────────────────

    except ProgrammingError:
        logger.exception(_CODE_ROUTES_ME)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_THIS_FEATURE_NOT_AVAILABLE,
        ) from None


class ContextSourceModeUpdate(BaseModel):
    source_mode: str = Field(..., pattern=r"^(always_on|tool|off)$")


@router.get("/me/remy/context-sources")
@handle_db_errors("me.get_user_context_sources")
async def get_user_context_sources(
    current_user: TenantPrincipal = Depends(get_current_tenant_user),
    session: AsyncSession = Depends(get_db_session),
) -> list[ContextSourceResponseItem]:
    try:
        async with session.begin():
            service = RemyContextSourceService(session)
            config = await service.get_effective_config(current_user.organisation_id, current_user.account_id)
            user_overrides = await service.get_user_overrides(current_user.organisation_id, current_user.account_id)
    except ProgrammingError:
        logger.exception(_CODE_ROUTES_ME)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_THIS_FEATURE_NOT_AVAILABLE,
        ) from None

    return service.build_effective_items(config.context_sources, user_overrides)


@router.put("/me/remy/context-sources/{source_key}")
@handle_db_errors("me.set_user_context_source")
async def set_user_context_source(
    source_key: str,
    req: ContextSourceModeUpdate,
    current_user: TenantPrincipal = Depends(get_current_tenant_user),
    session: AsyncSession = Depends(get_db_session),
) -> list[ContextSourceResponseItem]:
    try:
        async with session.begin():
            service = RemyContextSourceService(session)
            await service.set_user_override(
                current_user.organisation_id,
                current_user.account_id,
                source_key,
                req.source_mode,
            )
            config = await service.get_effective_config(current_user.organisation_id, current_user.account_id)
            user_overrides = await service.get_user_overrides(current_user.organisation_id, current_user.account_id)
    except ProgrammingError:
        logger.exception(_CODE_ROUTES_ME)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_THIS_FEATURE_NOT_AVAILABLE,
        ) from None

    return service.build_effective_items(config.context_sources, user_overrides)


@router.delete("/me/remy/context-sources", status_code=status.HTTP_200_OK)
@handle_db_errors("me.reset_user_context_sources")
async def reset_user_context_sources(
    current_user: TenantPrincipal = Depends(get_current_tenant_user),
    session: AsyncSession = Depends(get_db_session),
) -> list[ContextSourceResponseItem]:
    try:
        async with session.begin():
            service = RemyContextSourceService(session)
            await service.reset_user_overrides(current_user.organisation_id, current_user.account_id)
            config = await service.get_effective_config(current_user.organisation_id, current_user.account_id)
    except ProgrammingError:
        logger.exception(_CODE_ROUTES_ME)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_THIS_FEATURE_NOT_AVAILABLE,
        ) from None

    return service.build_effective_items(config.context_sources, {})
