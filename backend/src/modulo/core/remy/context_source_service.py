from __future__ import annotations

import uuid

from pydantic import BaseModel
from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.remy.config_service import RemyConfig
from modulo.db.models.remy_context_source import RemyContextSource

_VALID_SOURCE_MODES = {"always_on", "tool", "off"}

_BUILTIN_DEFAULTS: dict[str, str] = {
    "page_context": "always_on",
    "user_profile": "always_on",
    "product_primer": "always_on",
    "product_docs": "tool",
    "integration_status": "tool",
    "org_config": "tool",
    "feature_overview": "tool",
}

_BUILTIN_SOURCE_METADATA: dict[str, dict[str, str]] = {
    "page_context": {
        "name": "Page Context",
        "description": "Content and state of the current page Remy is viewing",
    },
    "user_profile": {
        "name": "User Profile",
        "description": "Your account details, name, and preferences",
    },
    "product_primer": {
        "name": "Product Primer",
        "description": "Overview of Modulo's features, capabilities, and architecture",
    },
    "product_docs": {
        "name": "Product Docs",
        "description": "Product surface and navigation from the product manifest",
    },
    "integration_status": {
        "name": "Integration Status",
        "description": "Status of connected integrations, connectors, and model backends",
    },
    "org_config": {
        "name": "Org Config",
        "description": "Organisation-level configuration settings and preferences",
    },
    "feature_overview": {
        "name": "Feature Overview",
        "description": "Available features based on your current plan tier",
    },
}


class ContextSourceResponseItem(BaseModel):
    key: str
    name: str
    description: str
    source_mode: str
    is_overridden: bool


class RemyContextSourceService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_org_defaults(self, org_id: uuid.UUID) -> dict[str, str]:
        return await self._query_by_user_id(org_id, user_id=None)

    async def get_user_overrides(self, org_id: uuid.UUID, user_id: uuid.UUID) -> dict[str, str]:
        return await self._query_by_user_id(org_id, user_id=user_id)

    async def _query_by_user_id(self, org_id: uuid.UUID, user_id: uuid.UUID | None) -> dict[str, str]:
        if user_id is None:
            stmt = select(RemyContextSource).where(
                RemyContextSource.organisation_id == org_id,
                RemyContextSource.user_id.is_(None),
            )
        else:
            stmt = select(RemyContextSource).where(
                RemyContextSource.organisation_id == org_id,
                RemyContextSource.user_id == user_id,
            )
        result = await self._session.execute(stmt)
        rows = list(result.scalars())
        return {r.source_key: r.source_mode for r in rows}

    def build_effective_items(
        self, effective: dict[str, str], user_overrides: dict[str, str]
    ) -> list[ContextSourceResponseItem]:
        return [
            ContextSourceResponseItem(
                key=key,
                name=_BUILTIN_SOURCE_METADATA.get(key, {}).get("name", key.replace("_", " ").title()),
                description=_BUILTIN_SOURCE_METADATA.get(key, {}).get("description", ""),
                source_mode=mode,
                is_overridden=key in user_overrides,
            )
            for key, mode in effective.items()
        ]

    async def get_effective_config(self, org_id: uuid.UUID, user_id: uuid.UUID) -> RemyConfig:
        config = RemyConfig()
        merged: dict[str, str] = dict(_BUILTIN_DEFAULTS)
        org_overrides = await self.get_org_defaults(org_id)
        merged.update(org_overrides)
        user_overrides = await self.get_user_overrides(org_id, user_id)
        merged.update(user_overrides)
        config.context_sources = merged
        return config

    async def _upsert_context_source(
        self, org_id: uuid.UUID, source_key: str, source_mode: str, user_id: uuid.UUID | None
    ) -> None:
        if not source_key:
            raise ValueError("source_key must not be empty")
        if source_mode not in _VALID_SOURCE_MODES:
            raise ValueError(f"Invalid source_mode '{source_mode}'. Must be one of {sorted(_VALID_SOURCE_MODES)}")
        if user_id is None:
            stmt = (
                select(RemyContextSource)
                .where(
                    RemyContextSource.organisation_id == org_id,
                    RemyContextSource.source_key == source_key,
                    RemyContextSource.user_id.is_(None),
                )
                .with_for_update()
            )
        else:
            stmt = (
                select(RemyContextSource)
                .where(
                    RemyContextSource.organisation_id == org_id,
                    RemyContextSource.source_key == source_key,
                    RemyContextSource.user_id == user_id,
                )
                .with_for_update()
            )
        result = await self._session.execute(stmt)
        existing = result.scalar_one_or_none()
        if existing:
            existing.source_mode = source_mode
        else:
            self._session.add(
                RemyContextSource(
                    id=uuid.uuid4(),
                    organisation_id=org_id,
                    user_id=user_id,
                    source_key=source_key,
                    source_mode=source_mode,
                )
            )
        await self._session.flush()

    async def set_user_override(self, org_id: uuid.UUID, user_id: uuid.UUID, source_key: str, source_mode: str) -> None:
        await self._upsert_context_source(org_id, source_key, source_mode, user_id=user_id)

    async def set_org_default(self, org_id: uuid.UUID, source_key: str, source_mode: str) -> None:
        await self._upsert_context_source(org_id, source_key, source_mode, user_id=None)

    async def reset_user_overrides(self, org_id: uuid.UUID, user_id: uuid.UUID) -> None:
        stmt = sa_delete(RemyContextSource).where(
            RemyContextSource.organisation_id == org_id,
            RemyContextSource.user_id == user_id,
        )
        await self._session.execute(stmt)
        await self._session.flush()
