from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.system_config import get_config, update_config

logger = logging.getLogger(__name__)


def _normalize_uuids(items: list[Any] | None) -> list[uuid.UUID]:
    if not items:
        return []
    result: list[uuid.UUID] = []
    for item in items:
        try:
            if isinstance(item, str):
                result.append(uuid.UUID(item))
            elif isinstance(item, uuid.UUID):
                result.append(item)
            else:
                logger.warning("Skipping unexpected type in UUID list: %s (%s)", item, type(item).__name__)
        except (ValueError, TypeError):
            logger.warning("Skipping invalid UUID in config access list: %s", item)
    return result


class AssistantConfig(BaseModel):
    schema_version: int = 3
    system_prompt: str = ""
    additional_guidance: str = ""
    product_primer: str = ""
    access_rules: dict[str, list[Any]] = Field(
        default_factory=lambda: {"user_ids": [], "team_ids": [], "org_roles": ["admin"]}
    )
    default_provider: str = "anthropic"
    default_model: str = "claude-sonnet-4-20250514"
    default_context_window: int = 200000
    allowed_providers: list[str] = Field(default_factory=lambda: ["anthropic", "openai", "gemini", "deepseek", "groq"])
    allowed_models: list[str] = Field(default_factory=list)  # empty = all models for allowed providers
    context_sources: dict[str, str] = Field(
        default_factory=lambda: {
            "page_context": "always_on",
            "user_profile": "always_on",
            "product_primer": "always_on",
            "product_docs": "tool",
            "integration_status": "tool",
            "org_config": "tool",
            "feature_overview": "tool",
        }
    )


_CONFIG_KEY_PREFIX = "assistant_config:"


class AssistantConfigService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_config(self, org_id: uuid.UUID) -> AssistantConfig:
        try:
            row = await get_config(self._session, f"{_CONFIG_KEY_PREFIX}{org_id}")
        except SQLAlchemyError:
            logger.exception("Failed to fetch Assistant config for org %s", org_id)
            return AssistantConfig()

        if row is None or not isinstance(row.value, dict):
            return AssistantConfig()
        try:
            return AssistantConfig(**row.value)
        except (ValueError, TypeError):
            logger.exception("Failed to parse stored Assistant config for org %s, falling back to defaults", org_id)
            return AssistantConfig()

    async def update_config(self, org_id: uuid.UUID, config: AssistantConfig) -> None:
        try:
            await update_config(
                self._session,
                key=f"{_CONFIG_KEY_PREFIX}{org_id}",
                value=config.model_dump(),
            )
            await self._session.flush()
        except SQLAlchemyError:
            logger.exception("Failed to update Assistant config for org %s", org_id)
            raise

    async def check_access(
        self,
        org_id: uuid.UUID,
        user_id: uuid.UUID,
        user_role: str,
        team_ids: list[uuid.UUID],
    ) -> bool:
        try:
            config = await self.get_config(org_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Failed to fetch config for access check, denying org %s", org_id)
            return False

        access = config.access_rules

        allowed_user_ids = _normalize_uuids(access.get("user_ids", []))
        if user_id in allowed_user_ids:
            return True

        if user_role.lower() in (r.lower() for r in (access.get("org_roles") or [])):
            return True

        allowed_team_ids = _normalize_uuids(access.get("team_ids", []))
        return bool(any(tid in allowed_team_ids for tid in team_ids))
