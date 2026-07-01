from __future__ import annotations

import logging
import uuid
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.system_config import get_config, set_config

logger = logging.getLogger(__name__)


class RemyConfig(BaseModel):
    system_prompt: str = ""
    additional_guidance: str = ""
    access_list: dict[str, list[Any]] = Field(
        default_factory=lambda: {"user_ids": [], "team_ids": [], "org_roles": ["admin"]}
    )
    default_provider: str = "anthropic"
    default_model: str = "claude-sonnet-4-20250514"
    default_context_window: int = 200000
    allowed_providers: list[str] = ["anthropic", "openai", "google-gemini", "deepseek", "groq"]
    allowed_models: list[str] = []  # empty = all models for allowed providers


_CONFIG_KEY_PREFIX = "remy_config:"


class RemyConfigService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_config(self, org_id: uuid.UUID) -> RemyConfig:
        row = await get_config(self._session, f"{_CONFIG_KEY_PREFIX}{org_id}")
        if row is None:
            return RemyConfig()
        if isinstance(row.value, dict):
            try:
                return RemyConfig(**row.value)
            except Exception:
                logger.exception("Failed to parse stored Remy config for org %s, falling back to defaults", org_id)
                return RemyConfig()
        return RemyConfig()

    async def update_config(self, org_id: uuid.UUID, config: RemyConfig) -> None:
        await set_config(
            self._session,
            key=f"{_CONFIG_KEY_PREFIX}{org_id}",
            value=config.model_dump(),
        )

    async def check_access(
        self,
        org_id: uuid.UUID,
        user_id: uuid.UUID,
        user_role: str,
        team_ids: list[uuid.UUID],
    ) -> bool:
        config = await self.get_config(org_id)
        access = config.access_list

        if user_id in [uuid.UUID(uid) if isinstance(uid, str) else uid for uid in access.get("user_ids", [])]:
            return True

        if user_role in access.get("org_roles", []):
            return True

        allowed_team_ids = [
            uuid.UUID(tid) if isinstance(tid, str) else tid
            for tid in access.get("team_ids", [])
        ]
        if any(tid in allowed_team_ids for tid in team_ids):
            return True

        return False
