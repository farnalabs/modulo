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
from modulo.db.models.remy_skill import RemySkill

logger = logging.getLogger(__name__)

_SECTION_ORG_SKILLS = "## Organisation Skills"
_SECTION_USER_SKILLS = "## User Skills"
_SECTION_PAGE_CONTEXT = "## Page Context"
_DELIMITER = "---"
_DELIMITER_LEN = 3


class SkillEntry(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None = None
    triggers: list[str] | None = None
    body: str
    frontmatter: dict[str, Any] | None = None


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

        parts: list[str] = []

        if config:
            base_prompt = system_prompt_override if system_prompt_override is not None else config.system_prompt
            if base_prompt:
                parts.append(base_prompt)

            if config.additional_guidance:
                parts.append(config.additional_guidance)

        if page_context:
            parts.append(f"{_SECTION_PAGE_CONTEXT}\n\n{page_context}")

        org_skills = await self.get_org_skills(org_id)
        self._append_skills_block(parts, org_skills, _SECTION_ORG_SKILLS)

        user_skills = await self.get_user_skills(user_id)
        self._append_skills_block(parts, user_skills, _SECTION_USER_SKILLS)

        if include_ui_tools_text:
            tools_text = build_tool_definitions_for_text()
            if tools_text:
                parts.append(tools_text)

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
        )
