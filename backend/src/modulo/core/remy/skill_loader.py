from __future__ import annotations

import logging
import uuid
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.remy.config_service import RemyConfigService
from modulo.db.models.remy_skill import RemySkill

logger = logging.getLogger(__name__)


class SkillEntry(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None = None
    triggers: list[str] | None = None
    body: str
    frontmatter: dict[str, Any] | None = None


class SkillLoader:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_org_skills(self, org_id: uuid.UUID) -> list[SkillEntry]:
        result = await self._session.execute(
            select(RemySkill).where(
                RemySkill.organisation_id == org_id,
                RemySkill.active.is_(True),
            )
        )
        return [self._to_entry(s) for s in result.scalars().all()]

    async def get_user_skills(self, user_id: uuid.UUID) -> list[SkillEntry]:
        result = await self._session.execute(
            select(RemySkill).where(
                RemySkill.user_id == user_id,
                RemySkill.active.is_(True),
            )
        )
        return [self._to_entry(s) for s in result.scalars().all()]

    async def build_system_prompt(
        self,
        org_id: uuid.UUID,
        user_id: uuid.UUID,
        page_context: str | None = None,
    ) -> str:
        config_service = RemyConfigService(self._session)
        config = await config_service.get_config(org_id)

        parts: list[str] = []

        if config.system_prompt:
            parts.append(config.system_prompt)

        if config.additional_guidance:
            parts.append(config.additional_guidance)

        if page_context:
            parts.append(f"## Page Context\n\n{page_context}")

        org_skills = await self.get_org_skills(org_id)
        if org_skills:
            parts.append("## Organisation Skills\n\n")
            for skill in org_skills:
                parts.append(f"### {skill.name}\n\n{skill.body}\n")

        user_skills = await self.get_user_skills(user_id)
        if user_skills:
            parts.append("## User Skills\n\n")
            for skill in user_skills:
                parts.append(f"### {skill.name}\n\n{skill.body}\n")

        return "\n\n".join(parts)

    @staticmethod
    def parse_skill_markdown(markdown: str) -> tuple[dict[str, Any] | None, str]:
        stripped = markdown.lstrip()
        if not stripped.startswith("---"):
            return None, markdown

        end_idx = stripped.find("---", 3)
        if end_idx == -1:
            return None, markdown

        frontmatter_text = stripped[3:end_idx].strip()
        body = stripped[end_idx + 3 :].lstrip()

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
            body=body if fm else skill.body,
            frontmatter=fm,
        )
