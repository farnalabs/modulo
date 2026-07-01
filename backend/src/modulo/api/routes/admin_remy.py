"""Admin Remy configuration and skills management."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.dependencies import get_db_session
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.db.models.remy_skill import RemySkill
from modulo.db.models.system_config import SystemConfig
from modulo.db.rls import set_rls_org

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/admin/remy", tags=["admin-remy"])


def _require_admin(principal: AuthenticatedPrincipal) -> None:
    if principal.org_role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin role required",
        )


# ── Config models ──────────────────────────────────────────────────────


class AccessList(BaseModel):
    user_ids: list[str] = Field(default_factory=list)
    team_ids: list[str] = Field(default_factory=list)
    org_roles: list[str] = Field(default_factory=list)


class RemyConfigResponse(BaseModel):
    system_prompt: str | None = None
    additional_guidance: str | None = None
    access_list: AccessList = Field(default_factory=AccessList)
    default_provider: str = "anthropic"
    default_model: str = "claude-sonnet-4-20250514"
    default_context_window: int = 200000
    allowed_providers: list[str] = ["anthropic", "openai", "google-gemini", "deepseek", "groq"]
    allowed_models: list[str] = []


class RemyConfigUpdate(BaseModel):
    system_prompt: str | None = None
    additional_guidance: str | None = None
    access_list: AccessList | None = None
    default_provider: str | None = None
    default_model: str | None = None
    default_context_window: int | None = None
    allowed_providers: list[str] | None = None
    allowed_models: list[str] | None = None


# ── Skill models (shared with user endpoints) ─────────────────────────


class SkillCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: str | None = None
    triggers: list[str] | None = None
    body: str
    active: bool = True


class SkillUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=100)
    description: str | None = None
    triggers: list[str] | None = None
    body: str | None = None
    active: bool | None = None


class SkillResponse(BaseModel):
    id: str
    name: str
    description: str | None
    triggers: list[str] | None
    body: str
    active: bool
    created_at: str
    updated_at: str


# ── Config endpoints ──────────────────────────────────────────────────


@router.get("/config", response_model=RemyConfigResponse)
async def get_remy_config(
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> RemyConfigResponse:
    _require_admin(principal)
    try:
        async with session.begin():
            result = await session.execute(
                select(SystemConfig).where(SystemConfig.key == f"remy_config:{principal.organisation_id}")
            )
            entry = result.scalar_one_or_none()
        if entry is None:
            return RemyConfigResponse()
        value = entry.value if isinstance(entry.value, dict) else {}
        return RemyConfigResponse(
            system_prompt=value.get("system_prompt"),
            additional_guidance=value.get("additional_guidance"),
            access_list=AccessList(**value.get("access_list", {})),
            default_provider=value.get("default_provider", "anthropic"),
            default_model=value.get("default_model", "claude-sonnet-4-20250514"),
            default_context_window=value.get("default_context_window", 200000),
            allowed_providers=value.get(
                "allowed_providers", ["anthropic", "openai", "google-gemini", "deepseek", "groq"]
            ),
            allowed_models=value.get("allowed_models", []),
        )
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None


@router.put("/config", response_model=RemyConfigResponse)
async def update_remy_config(
    body: RemyConfigUpdate,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> RemyConfigResponse:
    _require_admin(principal)
    try:
        async with session.begin():
            result = await session.execute(
                select(SystemConfig).where(SystemConfig.key == f"remy_config:{principal.organisation_id}")
            )
            entry = result.scalar_one_or_none()
            if entry is None:
                entry = SystemConfig(key=f"remy_config:{principal.organisation_id}", value={})
                session.add(entry)

            current: dict[str, Any] = entry.value if isinstance(entry.value, dict) else {}
            if body.system_prompt is not None:
                current["system_prompt"] = body.system_prompt
            if body.additional_guidance is not None:
                current["additional_guidance"] = body.additional_guidance
            if body.access_list is not None:
                current["access_list"] = body.access_list.model_dump()
            if body.default_provider is not None:
                current["default_provider"] = body.default_provider
            if body.default_model is not None:
                current["default_model"] = body.default_model
            if body.default_context_window is not None:
                current["default_context_window"] = body.default_context_window
            if body.allowed_providers is not None:
                current["allowed_providers"] = body.allowed_providers
            if body.allowed_models is not None:
                current["allowed_models"] = body.allowed_models
            entry.updated_by = principal.account_id
            entry.value = current
            await session.flush()

        return RemyConfigResponse(
            system_prompt=current.get("system_prompt"),
            additional_guidance=current.get("additional_guidance"),
            access_list=AccessList(**current.get("access_list", {})),
            default_provider=current.get("default_provider", "anthropic"),
            default_model=current.get("default_model", "claude-sonnet-4-20250514"),
            default_context_window=current.get("default_context_window", 200000),
            allowed_providers=current.get(
                "allowed_providers", ["anthropic", "openai", "google-gemini", "deepseek", "groq"]
            ),
            allowed_models=current.get("allowed_models", []),
        )
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None


# ── Org-level Skills CRUD ─────────────────────────────────────────────


async def _get_org_skill(
    session: AsyncSession,
    skill_id: uuid.UUID,
    org_id: uuid.UUID,
) -> RemySkill:
    skill = await session.get(RemySkill, skill_id)
    if skill is None or skill.organisation_id != org_id or skill.user_id is not None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Skill not found",
        )
    return skill


def _skill_to_response(skill: RemySkill) -> SkillResponse:
    return SkillResponse(
        id=str(skill.id),
        name=skill.name,
        description=skill.description,
        triggers=skill.triggers,
        body=skill.body,
        active=skill.active,
        created_at=skill.created_at.isoformat() if skill.created_at else "",
        updated_at=skill.updated_at.isoformat() if skill.updated_at else "",
    )


@router.get("/skills", response_model=list[SkillResponse])
async def list_org_skills(
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> list[SkillResponse]:
    _require_admin(principal)
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            result = await session.execute(
                select(RemySkill)
                .where(
                    RemySkill.organisation_id == principal.organisation_id,
                    RemySkill.user_id.is_(None),
                )
                .order_by(RemySkill.created_at.desc())
            )
            skills = list(result.scalars())
        return [_skill_to_response(s) for s in skills]
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None


@router.post("/skills", response_model=SkillResponse, status_code=status.HTTP_201_CREATED)
async def create_org_skill(
    body: SkillCreate,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> SkillResponse:
    _require_admin(principal)
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            skill = RemySkill(
                id=uuid.uuid4(),
                organisation_id=principal.organisation_id,
                user_id=None,
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


@router.put("/skills/{skill_id}", response_model=SkillResponse)
async def update_org_skill(
    skill_id: uuid.UUID,
    body: SkillUpdate,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> SkillResponse:
    _require_admin(principal)
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            skill = await _get_org_skill(session, skill_id, principal.organisation_id)
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


@router.delete("/skills/{skill_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_org_skill(
    skill_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: AuthenticatedPrincipal = Depends(get_current_user),
) -> None:
    _require_admin(principal)
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            skill = await _get_org_skill(session, skill_id, principal.organisation_id)
            await session.delete(skill)
    except ProgrammingError:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Feature is not available. Run database migrations to enable it.",
        ) from None


# ── User-level helper (reused by me.py) ────────────────────────────────


async def get_user_skills(session: AsyncSession, user_id: uuid.UUID) -> list[RemySkill]:
    result = await session.execute(
        select(RemySkill)
        .where(
            RemySkill.user_id == user_id,
            RemySkill.organisation_id.is_(None),
        )
        .order_by(RemySkill.created_at.desc())
    )
    return list(result.scalars())


async def get_user_skill_or_404(
    session: AsyncSession, user_id: uuid.UUID, skill_id: uuid.UUID
) -> RemySkill:
    skill = await session.get(RemySkill, skill_id)
    if skill is None or skill.user_id != user_id or skill.organisation_id is not None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Skill not found",
        )
    return skill
