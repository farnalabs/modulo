from __future__ import annotations

import logging
import uuid
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.ui_tools import build_tool_definitions_for_text
from modulo.core.remy.config_service import RemyConfigService
from modulo.core.remy.context_source_service import RemyContextSourceService
from modulo.db.models.remy_skill import RemySkill

logger = logging.getLogger(__name__)

_SECTION_ORG_SKILLS = "## Organisation Skills"
_SECTION_USER_SKILLS = "## User Skills"
_SECTION_PAGE_CONTEXT = "## Page Context"
_SECTION_PRODUCT_OVERVIEW = "## Product Overview"
_SECTION_USER_PROFILE = "## User Profile"
_SECTION_KNOWLEDGE_TOOLS = "## Available Knowledge Tools"
_DELIMITER = "---"
_DELIMITER_LEN = 3

# Tool descriptions for built-in context sources with tool mode
_TOOL_DESCRIPTIONS: dict[str, str] = {
    "product_docs": "get_documentation(query, section?) — Search product docs, FAQ, how-to guides",
    "integration_status": "get_integration_status() — Get connector and model backend health",
    "org_config": "get_org_config(section?) — Get org settings and feature flags",
    "feature_overview": "get_available_features() — Get feature availability by plan tier",
}


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
    ) -> None:
        self._session = session
        self._config_service = config_service

    async def _get_skills(self, **filters: Any) -> list[SkillEntry]:
        try:
            stmt = select(RemySkill).where(
                *[getattr(RemySkill, k) == v for k, v in filters.items()],
                RemySkill.active.is_(True),
            )
            result = await self._session.execute(stmt)
            return [self._to_entry(s) for s in result.scalars().all()]
        except SQLAlchemyError:
            logger.exception("Failed to query skills with filters %s", filters)
            return []

    async def get_org_skills(self, org_id: uuid.UUID) -> list[SkillEntry]:
        return await self._get_skills(organisation_id=org_id, user_id=None)

    async def get_user_skills(self, user_id: uuid.UUID) -> list[SkillEntry]:
        return await self._get_skills(user_id=user_id, organisation_id=None)

    def _append_skills_block(
        self, parts: list[str], skills: list[SkillEntry], heading: str
    ) -> None:
        if not skills:
            return
        parts.append(heading)
        for skill in skills:
            parts.append(f"### {skill.name}\n\n{skill.body}")

    async def _build_user_profile(self, org_id: uuid.UUID, user_id: uuid.UUID) -> str | None:
        from modulo.db.models.account import Account
        from modulo.db.models.org_membership import OrgMembership
        from modulo.db.models.organisation import Organisation

        try:
            acct_result = await self._session.execute(
                select(Account).where(Account.id == user_id)
            )
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

            org_result = await self._session.execute(
                select(Organisation).where(Organisation.id == org_id)
            )
            org = org_result.scalar_one_or_none()

            lines = [f"{_SECTION_USER_PROFILE}\n"]
            lines.append(f"- **Name:** {account.display_name}")
            lines.append(f"- **Email:** {account.email}")
            if membership:
                lines.append(f"- **Role:** {membership.role}")
            if org:
                lines.append(f"- **Organisation:** {org.name}")
                if org.plan_id:
                    lines.append(f"- **Plan:** {org.plan_id}")
            return "\n".join(lines)
        except Exception:
            logger.exception("Failed to build user profile for user %s", user_id)
            return None

    def _build_knowledge_tools_section(self, skills: list[SkillEntry], ctx_sources: dict[str, str]) -> str | None:
        lines: list[str] = []

        for source_key, mode in ctx_sources.items():
            if mode == "tool" and source_key in _TOOL_DESCRIPTIONS:
                lines.append(f"- {_TOOL_DESCRIPTIONS[source_key]}")

        tool_skills = [s for s in skills if s.source_mode == "tool"]
        if tool_skills:
            lines.append("- get_skill(name) — Load an organisation or personal skill by name")

        if not lines:
            return None

        return f"{_SECTION_KNOWLEDGE_TOOLS}\n\nYou can retrieve additional knowledge by calling these tools:\n" + "\n".join(lines) + "\n"

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
        except Exception:
            logger.exception("Failed to load Remy config for org %s", org_id)
            config = None

        ctx_service = RemyContextSourceService(self._session)
        try:
            effective = await ctx_service.get_effective_config(org_id, user_id)
        except Exception:
            logger.exception("Failed to load context source config for org %s", org_id)
            effective = None

        ctx_sources: dict[str, str] = effective.context_sources if effective else {}

        parts: list[str] = []

        # 1. Base admin system prompt
        if config:
            base_prompt = system_prompt_override if system_prompt_override is not None else config.system_prompt
            if base_prompt:
                parts.append(base_prompt)

        # 2. Additional guidance
            if config.additional_guidance:
                parts.append(config.additional_guidance)

        # 3. Product Overview
            if ctx_sources.get("product_primer") == "always_on" and config.product_primer:
                parts.append(f"{_SECTION_PRODUCT_OVERVIEW}\n\n{config.product_primer}")

        # 4. Page Context
        if ctx_sources.get("page_context") == "always_on" and page_context:
            parts.append(f"{_SECTION_PAGE_CONTEXT}\n\n{page_context}")

        # 5. User Profile
        if ctx_sources.get("user_profile") == "always_on":
            profile = await self._build_user_profile(org_id, user_id)
            if profile:
                parts.append(profile)

        # 6. Available Knowledge Tools
        org_skills = await self.get_org_skills(org_id)
        user_skills = await self.get_user_skills(user_id)

        tool_section = self._build_knowledge_tools_section(org_skills + user_skills, ctx_sources)
        if tool_section:
            parts.append(tool_section)

        # 7. Organisation Skills (source_mode = always_on or null)
        always_on_org = [s for s in org_skills if s.source_mode is None or s.source_mode == "always_on"]
        self._append_skills_block(parts, always_on_org, _SECTION_ORG_SKILLS)

        # 8. User Skills (source_mode = always_on or null)
        always_on_user = [s for s in user_skills if s.source_mode is None or s.source_mode == "always_on"]
        self._append_skills_block(parts, always_on_user, _SECTION_USER_SKILLS)

        if include_ui_tools_text:
            tools_text = build_tool_definitions_for_text()
            if tools_text:
                parts.append(tools_text)
                parts.append("- Before navigating, call get_manifest() to learn page structure and elements.")

        return "\n\n".join(parts)

    @staticmethod
    def parse_skill_markdown(markdown: str | None) -> tuple[dict[str, Any] | None, str]:
        if not markdown:
            return None, ""

        stripped = markdown.lstrip()
        if not stripped.startswith(_DELIMITER):
            return None, markdown

        end_idx = stripped.find(_DELIMITER, _DELIMITER_LEN)
        if end_idx == -1:
            return None, markdown

        frontmatter_text = stripped[_DELIMITER_LEN:end_idx].strip()
        body = stripped[end_idx + _DELIMITER_LEN :].lstrip()

        frontmatter: dict[str, Any] = {}
        for line in frontmatter_text.split("\n"):
            line = line.strip()
            if not line or ":" not in line:
                continue
            key, _, value = line.partition(":")
            key = key.strip()
            value = value.strip()

            if value.startswith("[") and value.endswith("]"):
                frontmatter[key] = [
                    v.strip().strip("\"'") for v in value[1:-1].split(",") if v.strip()
                ]
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
