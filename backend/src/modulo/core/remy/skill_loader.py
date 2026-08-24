from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.remy.config_service import RemyConfig, RemyConfigService
from modulo.core.remy.context_source_service import RemyContextSourceService
from modulo.db.models.account import Account
from modulo.db.models.org_membership import OrgMembership
from modulo.db.models.organisation import Organisation
from modulo.db.models.remy_skill import RemySkill

logger = logging.getLogger(__name__)

_SECTION_ORG_SKILLS = "## Organisation Skills"
_SECTION_USER_SKILLS = "## User Skills"
_SECTION_PAGE_CONTEXT = "## Page Context"
_SECTION_BEHAVIOR = "## Behaviour"
_SECTION_PRODUCT_OVERVIEW = "## Product Overview"
_SECTION_USER_PROFILE = "## User Profile"
_SECTION_KNOWLEDGE_TOOLS = "## Available Knowledge Tools"
_DELIMITER = "---"

# Tool descriptions for built-in context sources with tool mode
_TOOL_DESCRIPTIONS: dict[str, str] = {
    "product_docs": "search_documentation(query, section?) — Search product surface and navigation",
    "integration_status": "get_integration_status() — Get connector and model backend health",
    "org_config": "get_org_config(section?) — Get org settings and feature flags",
    "feature_overview": "get_available_features() — Get feature availability by plan tier",
}

_SKILL_PAGE_SIZE = 500


class SkillEntry(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None = None
    triggers: list[str] | None = None
    body: str
    frontmatter: dict[str, Any] | None = None
    source_mode: str | None = None


class SkillLoader:
    def __init__(
        self,
        session: AsyncSession,
        config_service: RemyConfigService | None = None,
        ctx_service: RemyContextSourceService | None = None,
        ui_tools_text_fn: Callable[[], str] | None = None,
    ) -> None:
        self._session = session
        self._config_service = config_service
        self._ctx_service = ctx_service
        self._ui_tools_text_fn = ui_tools_text_fn

    async def _get_skills(self, **filters: Any) -> list[SkillEntry]:
        try:
            conditions = []
            for k, v in filters.items():
                col = getattr(RemySkill, k, None)
                if col is None:
                    logger.warning("Unknown skill filter column: %s", k)
                    continue
                conditions.append(col == v)
            stmt = (
                select(RemySkill)
                .where(*conditions, RemySkill.active.is_(True))
                .order_by(RemySkill.id)
                .limit(_SKILL_PAGE_SIZE)
            )
            result = await self._session.execute(stmt)
            return [self._to_entry(s) for s in result.scalars().all()]
        except asyncio.CancelledError:
            raise
        except SQLAlchemyError:
            logger.exception("Failed to query skills with filters %s", filters)
            return []
        except Exception:
            logger.exception("Unexpected error querying skills with filters %s", filters)
            return []

    async def get_org_skills(self, org_id: uuid.UUID) -> list[SkillEntry]:
        return await self._get_skills(organisation_id=org_id, user_id=None)

    async def get_user_skills(self, user_id: uuid.UUID) -> list[SkillEntry]:
        return await self._get_skills(user_id=user_id, organisation_id=None)

    def _append_skills_block(self, parts: list[str], skills: list[SkillEntry], heading: str) -> None:
        if not skills:
            return
        parts.append(heading)
        parts.extend(f"### {skill.name}\n\n{skill.body}" for skill in skills)

    async def _build_user_profile(self, org_id: uuid.UUID, user_id: uuid.UUID) -> str | None:
        try:
            acct_result = await self._session.execute(select(Account).where(Account.id == user_id))
            account = acct_result.scalar_one_or_none()
            if not account:
                return None

            membership_result = await self._session.execute(
                select(OrgMembership).where(
                    OrgMembership.account_id == user_id,
                    OrgMembership.organisation_id == org_id,
                )
            )
            membership = membership_result.scalar_one_or_none()

            org_result = await self._session.execute(select(Organisation).where(Organisation.id == org_id))
            org = org_result.scalar_one_or_none()

            lines = [
                f"{_SECTION_USER_PROFILE}\n",
                f"- **Name:** {account.display_name or '—'}",
                f"- **Email:** {account.email or '—'}",
            ]
            if membership:
                lines.append(f"- **Role:** {membership.role}")
            if org:
                lines.append(f"- **Organisation:** {org.name}")
                if org.plan_id:
                    lines.append(f"- **Plan:** {org.plan_id}")
            return "\n".join(lines)
        except asyncio.CancelledError:
            raise
        except SQLAlchemyError:
            logger.exception("Failed to build user profile for user %s", user_id)
            return None
        except Exception:
            logger.exception("Unexpected error building user profile for user %s", user_id)
            return None

    def _build_knowledge_tools_section(self, skills: list[SkillEntry], ctx_sources: dict[str, str]) -> str | None:
        lines: list[str] = []

        for source_key, mode in ctx_sources.items():
            if mode == "tool":
                if source_key in _TOOL_DESCRIPTIONS:
                    lines.append(f"- {_TOOL_DESCRIPTIONS[source_key]}")
                else:
                    logger.debug("Context source key '%s' has no tool description, skipping", source_key)

        tool_skills = [s for s in skills if s.source_mode == "tool"]
        if tool_skills:
            lines.append("- get_skill(name) — Load an organisation or personal skill by name")

        if not lines:
            return None

        heading = f"{_SECTION_KNOWLEDGE_TOOLS}\n\nYou can retrieve additional knowledge by calling these tools:\n"
        return heading + "\n".join(lines) + "\n"

    def _build_config_section(self, config: RemyConfig | None, system_prompt_override: str | None) -> str | None:
        if config is None:
            return None
        base_prompt = system_prompt_override if system_prompt_override is not None else config.system_prompt
        return base_prompt or None

    def _build_guidance_section(self, config: RemyConfig | None) -> str | None:
        if config is None:
            return None
        return config.additional_guidance or None

    def _build_overview_section(self, config: RemyConfig | None, ctx_sources: dict[str, str]) -> str | None:
        if config is None:
            return None
        if ctx_sources.get("product_primer") == "always_on" and config.product_primer:
            return f"{_SECTION_PRODUCT_OVERVIEW}\n\n{config.product_primer}"
        return None

    def _build_page_context_section(self, ctx_sources: dict[str, str], page_context: str | None) -> str | None:
        if ctx_sources.get("page_context") == "always_on" and page_context:
            return f"{_SECTION_PAGE_CONTEXT}\n\n{page_context}"
        return None

    async def _build_profile_section(
        self, org_id: uuid.UUID, user_id: uuid.UUID, ctx_sources: dict[str, str]
    ) -> str | None:
        if ctx_sources.get("user_profile") == "always_on":
            return await self._build_user_profile(org_id, user_id)
        return None

    def _filter_always_on(self, skills: list[SkillEntry]) -> list[SkillEntry]:
        return [s for s in skills if s.source_mode is None or s.source_mode == "always_on"]

    async def build_system_prompt(
        self,
        org_id: uuid.UUID,
        user_id: uuid.UUID,
        page_context: str | None = None,
        system_prompt_override: str | None = None,
        include_ui_tools_text: bool = False,
    ) -> str:
        config_service = self._config_service or RemyConfigService(self._session)
        try:
            config = await config_service.get_config(org_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Failed to load Remy config for org %s", org_id)
            config = None

        ctx_service = self._ctx_service or RemyContextSourceService(self._session)
        try:
            effective = await ctx_service.get_effective_config(org_id, user_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Failed to load context source config for org %s", org_id)
            effective = None

        ctx_sources: dict[str, str] = effective.context_sources if effective else {}

        parts: list[str] = []

        base = self._build_config_section(config, system_prompt_override)
        if base:
            parts.append(base)

        guidance = self._build_guidance_section(config)
        if guidance:
            parts.append(guidance)

        overview = self._build_overview_section(config, ctx_sources)
        if overview:
            parts.append(overview)

        page_ctx = self._build_page_context_section(ctx_sources, page_context)
        if page_ctx:
            parts.append(page_ctx)

        parts.append(
            f"{_SECTION_BEHAVIOR}\n\n"
            "The user has direct visual access to the application UI. Do NOT dump tables, "
            "lists, or structured summaries of page content — the user can see the page "
            "themselves. You can reference what is visible (e.g. 'I can see 12 pipelines "
            "are running') but never reproduce the page verbatim. Keep responses concise."
        )

        profile = await self._build_profile_section(org_id, user_id, ctx_sources)
        if profile:
            parts.append(profile)

        org_skills = await self.get_org_skills(org_id)
        user_skills = await self.get_user_skills(user_id)

        tool_section = self._build_knowledge_tools_section(org_skills + user_skills, ctx_sources)
        if tool_section:
            parts.append(tool_section)

        always_on_org = self._filter_always_on(org_skills)
        self._append_skills_block(parts, always_on_org, _SECTION_ORG_SKILLS)

        always_on_user = self._filter_always_on(user_skills)
        self._append_skills_block(parts, always_on_user, _SECTION_USER_SKILLS)

        if include_ui_tools_text and self._ui_tools_text_fn:
            try:
                tools_text = self._ui_tools_text_fn()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Failed to build UI tools text")
                tools_text = None
            if tools_text:
                parts.extend(
                    [tools_text, "- Before navigating, call get_manifest() to learn page structure and elements."]
                )

        return "\n\n".join(parts)

    @staticmethod
    def parse_skill_markdown(markdown: str | None) -> tuple[dict[str, Any] | None, str]:
        if not markdown:
            return None, ""

        stripped = markdown.lstrip()
        if not stripped.startswith(_DELIMITER):
            return None, markdown

        end_idx = stripped.find(_DELIMITER, len(_DELIMITER))
        if end_idx == -1:
            return None, markdown

        frontmatter_text = stripped[len(_DELIMITER) : end_idx].strip()
        body = stripped[end_idx + len(_DELIMITER) :].lstrip()

        frontmatter: dict[str, Any] = {}
        for line in frontmatter_text.split("\n"):
            line = line.strip()
            if not line or ":" not in line:
                continue
            key, _, value = line.partition(":")
            key = key.strip()
            if not key:
                continue
            value = value.strip()

            if value.startswith("[") and value.endswith("]"):
                frontmatter[key] = [v.strip().strip("\"'") for v in value[1:-1].split(",") if v.strip()]
            elif value.lower() in ("true", "false"):
                frontmatter[key] = value.lower() == "true"
            else:
                frontmatter[key] = value.strip("\"'")

        return frontmatter, body

    def _to_entry(self, skill: RemySkill) -> SkillEntry:
        fm, body = self.parse_skill_markdown(skill.body)
        return SkillEntry(
            id=skill.id,
            name=skill.name,
            description=skill.description,
            triggers=skill.triggers,
            body=body if fm is not None else skill.body,
            frontmatter=fm,
            source_mode=skill.source_mode,
        )
