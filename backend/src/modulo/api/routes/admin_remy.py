"""Admin Remy configuration and skills management."""

from __future__ import annotations

import logging
import uuid
from typing import Any, ClassVar

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.constants import MSG_FEATURE_NOT_AVAILABLE, MSG_INTERNAL_SERVER_ERROR
from modulo.api.db_error_handling import handle_db_errors
from modulo.api.dependencies import get_db_session, require_permission
from modulo.auth.jwt import TenantPrincipal
from modulo.db.models.remy_skill import RemySkill
from modulo.db.models.system_config import SystemConfig
from modulo.db.rls import set_rls_org

_CODE_ADMIN_REMY_MANAGE = "admin.remy.manage"
_MSG_CLAUDE_SONNET_4_20250514 = "claude-sonnet-4-20250514"


# Labels for all known providers (both native and custom).
# Derived from the Remy runtime providers and ModelBackendProvider enum (custom).
logger = logging.getLogger(__name__)

_PROVIDER_LABELS: dict[str, str] = {
    "ai21": "AI21",
    "anthropic": "Anthropic",
    "deepseek": "DeepSeek",
    "fireworks": "Fireworks AI",
    "gemini": "Gemini",
    "grok": "Grok",
    "groq": "Groq",
    "openai": "OpenAI",
    "openrouter": "OpenRouter",
    "perplexity": "Perplexity",
    "qwen": "Qwen",
    "togetherai": "Together AI",
    "azure_openai": "Azure OpenAI",
    "bedrock": "Amazon Bedrock",
    "ollama": "Ollama",
    "opencode": "OpenCode",
    "cohere": "Cohere",
    "mistral": "Mistral",
    "replicate": "Replicate",
    "vertexai": "Vertex AI",
    "watsonx": "IBM watsonx",
    "vllm": "vLLM",
    "tgi": "TGI",
    "lm_studio": "LM Studio",
    "jan": "Jan",
    "localai": "LocalAI",
    "llamacpp": "llama.cpp",
    "custom": "Custom",
}

router = APIRouter(prefix="/api/v1/admin/remy", tags=["admin-remy"])


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
    default_model: str = _MSG_CLAUDE_SONNET_4_20250514
    default_context_window: int = 200000
    allowed_providers: list[str] = Field(default_factory=lambda: ["anthropic", "openai", "gemini", "deepseek", "groq"])
    allowed_models: ClassVar[list[str]] = []


class RemyConfigUpdate(BaseModel):
    system_prompt: str | None = None
    additional_guidance: str | None = None
    access_list: AccessList | None = None
    default_provider: str | None = None
    default_model: str | None = None
    default_context_window: int | None = None
    allowed_providers: list[str] | None = None
    allowed_models: list[str] | None = None


class AvailableProviderInfo(BaseModel):
    id: str
    label: str


class AvailableProvidersResponse(BaseModel):
    native: list[AvailableProviderInfo]
    custom_types: list[AvailableProviderInfo]


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


class ContextSourceModeUpdate(BaseModel):
    source_mode: str = Field(..., pattern=r"^(always_on|tool|off)$")


# ── Config endpoints ──────────────────────────────────────────────────


@router.get("/config")
@handle_db_errors("admin.remy.get_remy_config")
async def get_remy_config(
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_ADMIN_REMY_MANAGE),
) -> RemyConfigResponse:
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
            default_model=value.get("default_model", _MSG_CLAUDE_SONNET_4_20250514),
            default_context_window=value.get("default_context_window", 200000),
            allowed_providers=value.get("allowed_providers", ["anthropic", "openai", "gemini", "deepseek", "groq"]),
            allowed_models=value.get("allowed_models", []),
        )
    except ProgrammingError:
        logger.exception("admin_remy.get_remy_config")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        logger.exception("admin_remy.get_remy_config")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database error while fetching Remy config.",
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception("Unexpected error in get_remy_config")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None


@router.get("/available-providers")
@handle_db_errors("admin.remy.get_available_providers")
async def get_available_providers(
    principal: TenantPrincipal = require_permission(_CODE_ADMIN_REMY_MANAGE),
) -> AvailableProvidersResponse:
    try:
        from modulo.api.routes.remy import SUPPORTED_PROVIDERS
        from modulo.db.enums import ModelBackendProvider

        native_ids = set(SUPPORTED_PROVIDERS)
        native = [
            AvailableProviderInfo(id=k, label=_PROVIDER_LABELS.get(k, k.replace("_", " ").title()))
            for k in sorted(native_ids)
        ]
        custom_types = [
            AvailableProviderInfo(id=v.value, label=_PROVIDER_LABELS.get(v.value, v.name.replace("_", " ").title()))
            for v in ModelBackendProvider
            if v.value not in native_ids
        ]
        return AvailableProvidersResponse(native=native, custom_types=custom_types)
    except HTTPException:
        raise
    except Exception:
        logger.exception("Unexpected error in get_available_providers")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None


@router.put("/config")
@handle_db_errors("admin.remy.update_remy_config")
async def update_remy_config(
    req: RemyConfigUpdate,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_ADMIN_REMY_MANAGE),
) -> RemyConfigResponse:
    if req.allowed_providers is not None:
        from modulo.api.routes.remy import SUPPORTED_PROVIDERS

        invalid = [p for p in req.allowed_providers if p not in SUPPORTED_PROVIDERS]
        if invalid:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"Unsupported providers: {invalid}. Supported: {sorted(SUPPORTED_PROVIDERS)}",
            )
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
            if req.system_prompt is not None:
                current["system_prompt"] = req.system_prompt
            if req.additional_guidance is not None:
                current["additional_guidance"] = req.additional_guidance
            if req.access_list is not None:
                current["access_list"] = req.access_list.model_dump()
            if req.default_provider is not None:
                current["default_provider"] = req.default_provider
            if req.default_model is not None:
                current["default_model"] = req.default_model
            if req.default_context_window is not None:
                current["default_context_window"] = req.default_context_window
            if req.allowed_providers is not None:
                current["allowed_providers"] = req.allowed_providers
            if req.allowed_models is not None:
                current["allowed_models"] = req.allowed_models
            entry.updated_by = principal.account_id
            entry.value = current
            await session.flush()

        return RemyConfigResponse(
            system_prompt=current.get("system_prompt"),
            additional_guidance=current.get("additional_guidance"),
            access_list=AccessList(**current.get("access_list", {})),
            default_provider=current.get("default_provider", "anthropic"),
            default_model=current.get("default_model", _MSG_CLAUDE_SONNET_4_20250514),
            default_context_window=current.get("default_context_window", 200000),
            allowed_providers=current.get("allowed_providers", ["anthropic", "openai", "gemini", "deepseek", "groq"]),
            allowed_models=current.get("allowed_models", []),
        )
    except ProgrammingError:
        logger.exception("admin_remy.update_remy_config")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        logger.exception("admin_remy.update_remy_config")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database error while updating Remy config.",
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception("Unexpected error in update_remy_config")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
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


@router.get("/skills")
@handle_db_errors("admin.remy.list_org_skills")
async def list_org_skills(
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_ADMIN_REMY_MANAGE),
) -> list[SkillResponse]:
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
        logger.exception("admin_remy.list_org_skills")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        logger.exception("admin_remy.list_org_skills")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database error while listing skills.",
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception("Unexpected error in list_org_skills")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None


@router.post("/skills", status_code=status.HTTP_201_CREATED)
@handle_db_errors("admin.remy.create_org_skill")
async def create_org_skill(
    req: SkillCreate,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_ADMIN_REMY_MANAGE),
) -> SkillResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            skill = RemySkill(
                id=uuid.uuid4(),
                organisation_id=principal.organisation_id,
                user_id=None,
                name=req.name,
                description=req.description,
                triggers=req.triggers,
                body=req.body,
                active=req.active,
            )
            session.add(skill)
            await session.flush()
        return _skill_to_response(skill)
    except ProgrammingError:
        logger.exception("admin_remy.create_org_skill")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        logger.exception("admin_remy.create_org_skill")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database error while creating skill.",
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception("Unexpected error in create_org_skill")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None


@router.put("/skills/{skill_id}")
@handle_db_errors("admin.remy.update_org_skill")
async def update_org_skill(
    skill_id: uuid.UUID,
    req: SkillUpdate,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_ADMIN_REMY_MANAGE),
) -> SkillResponse:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            skill = await _get_org_skill(session, skill_id, principal.organisation_id)
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
        return _skill_to_response(skill)
    except ProgrammingError:
        logger.exception("admin_remy.update_org_skill")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        logger.exception("admin_remy.update_org_skill")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database error while updating skill.",
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception("Unexpected error in update_org_skill")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None


@router.delete("/skills/{skill_id}", status_code=status.HTTP_204_NO_CONTENT)
@handle_db_errors("admin.remy.delete_org_skill")
async def delete_org_skill(
    skill_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_ADMIN_REMY_MANAGE),
) -> None:
    try:
        async with session.begin():
            await set_rls_org(session, principal.organisation_id)
            skill = await _get_org_skill(session, skill_id, principal.organisation_id)
            await session.delete(skill)
    except ProgrammingError:
        logger.exception("admin_remy.delete_org_skill")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        logger.exception("admin_remy.delete_org_skill")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database error while deleting skill.",
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception("Unexpected error in delete_org_skill")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None


# ── Org-level Context Sources ─────────────────────────────────────────


@router.get("/context-sources")
@handle_db_errors("admin.remy.get_org_context_sources")
async def get_org_context_sources(
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_ADMIN_REMY_MANAGE),
) -> dict[str, object]:
    try:
        async with session.begin():
            from modulo.core.remy.context_source_service import (
                RemyContextSourceService,
            )

            service = RemyContextSourceService(session)
            org_defaults = await service.get_org_defaults(principal.organisation_id)
            from modulo.core.remy.config_service import RemyConfig

            builtin_defaults = RemyConfig().context_sources
        return {
            "builtin_defaults": builtin_defaults,
            "org_overrides": org_defaults,
            "effective": {**builtin_defaults, **org_defaults},
        }
    except ProgrammingError:
        logger.exception("admin_remy.get_org_context_sources")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        logger.exception("admin_remy.get_org_context_sources")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database error while fetching context sources.",
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception("Unexpected error in get_org_context_sources")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None


@router.put("/context-sources/{source_key}")
@handle_db_errors("admin.remy.set_org_context_source")
async def set_org_context_source(
    source_key: str,
    req: ContextSourceModeUpdate,
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_ADMIN_REMY_MANAGE),
) -> dict[str, str]:
    try:
        async with session.begin():
            from modulo.core.remy.context_source_service import (
                RemyContextSourceService,
            )

            service = RemyContextSourceService(session)
            await service.set_org_default(principal.organisation_id, source_key, req.source_mode)
            return await service.get_org_defaults(principal.organisation_id)
    except ProgrammingError:
        logger.exception("admin_remy.set_org_context_source")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        logger.exception("admin_remy.set_org_context_source")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database error while updating context source.",
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception("Unexpected error in set_org_context_source")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None


@router.delete("/context-sources", status_code=status.HTTP_200_OK)
@handle_db_errors("admin.remy.reset_org_context_sources")
async def reset_org_context_sources(
    session: AsyncSession = Depends(get_db_session),
    principal: TenantPrincipal = require_permission(_CODE_ADMIN_REMY_MANAGE),
) -> dict[str, str]:
    try:
        async with session.begin():
            from modulo.db.models.remy_context_source import RemyContextSource

            result = await session.execute(
                select(RemyContextSource).where(
                    RemyContextSource.organisation_id == principal.organisation_id,
                    RemyContextSource.user_id.is_(None),
                )
            )
            rows = list(result.scalars())
            for row in rows:
                await session.delete(row)
        return {}
    except ProgrammingError:
        logger.exception("admin_remy.reset_org_context_sources")
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=MSG_FEATURE_NOT_AVAILABLE,
        ) from None
    except SQLAlchemyError:
        logger.exception("admin_remy.reset_org_context_sources")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database error while resetting context sources.",
        ) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception("Unexpected error in reset_org_context_sources")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=MSG_INTERNAL_SERVER_ERROR,
        ) from None


# ── User-level helper (reused by me.py) ────────────────────────────────


async def get_user_skills(session: AsyncSession, user_id: uuid.UUID, org_id: uuid.UUID) -> list[RemySkill]:
    result = await session.execute(
        select(RemySkill)
        .where(
            or_(
                RemySkill.user_id == user_id,
                RemySkill.organisation_id == org_id,
            )
        )
        .order_by(RemySkill.created_at.desc())
    )
    return list(result.scalars())


async def get_user_skill_or_404(session: AsyncSession, user_id: uuid.UUID, skill_id: uuid.UUID) -> RemySkill:
    skill = await session.get(RemySkill, skill_id)
    if skill is None or skill.user_id != user_id or skill.organisation_id is not None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Skill not found",
        )
    return skill
