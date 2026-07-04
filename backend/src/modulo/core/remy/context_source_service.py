from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.remy.config_service import RemyConfig
from modulo.db.models.remy_context_source import RemyContextSource


_BUILTIN_DEFAULTS: dict[str, str] = {
    "page_context": "always_on",
    "user_profile": "always_on",
    "product_primer": "always_on",
    "product_docs": "tool",
    "integration_status": "tool",
    "org_config": "tool",
    "feature_overview": "tool",
}


class RemyContextSourceService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_org_defaults(self, org_id: uuid.UUID) -> dict[str, str]:
        result = await self._session.execute(
            select(RemyContextSource).where(
                RemyContextSource.organisation_id == org_id,
                RemyContextSource.user_id.is_(None),
            )
        )
        rows = list(result.scalars())
        return {r.source_key: r.source_mode for r in rows}

    async def get_user_overrides(self, org_id: uuid.UUID, user_id: uuid.UUID) -> dict[str, str]:
        result = await self._session.execute(
            select(RemyContextSource).where(
                RemyContextSource.organisation_id == org_id,
                RemyContextSource.user_id == user_id,
            )
        )
        rows = list(result.scalars())
        return {r.source_key: r.source_mode for r in rows}

    async def get_effective_config(self, org_id: uuid.UUID, user_id: uuid.UUID) -> RemyConfig:
        config = RemyConfig()
        merged: dict[str, str] = dict(_BUILTIN_DEFAULTS)
        org_overrides = await self.get_org_defaults(org_id)
        merged.update(org_overrides)
        user_overrides = await self.get_user_overrides(org_id, user_id)
        merged.update(user_overrides)
        config.context_sources = merged
        return config

    async def set_user_override(
        self, org_id: uuid.UUID, user_id: uuid.UUID, source_key: str, source_mode: str
    ) -> None:
        result = await self._session.execute(
            select(RemyContextSource).where(
                RemyContextSource.organisation_id == org_id,
                RemyContextSource.user_id == user_id,
                RemyContextSource.source_key == source_key,
            )
        )
        entry = result.scalar_one_or_none()
        if entry is None:
            entry = RemyContextSource(
                id=uuid.uuid4(),
                organisation_id=org_id,
                user_id=user_id,
                source_key=source_key,
                source_mode=source_mode,
            )
            self._session.add(entry)
        else:
            entry.source_mode = source_mode

    async def set_org_default(
        self, org_id: uuid.UUID, source_key: str, source_mode: str
    ) -> None:
        result = await self._session.execute(
            select(RemyContextSource).where(
                RemyContextSource.organisation_id == org_id,
                RemyContextSource.user_id.is_(None),
                RemyContextSource.source_key == source_key,
            )
        )
        entry = result.scalar_one_or_none()
        if entry is None:
            entry = RemyContextSource(
                id=uuid.uuid4(),
                organisation_id=org_id,
                user_id=None,
                source_key=source_key,
                source_mode=source_mode,
            )
            self._session.add(entry)
        else:
            entry.source_mode = source_mode

    async def reset_user_overrides(self, org_id: uuid.UUID, user_id: uuid.UUID) -> None:
        result = await self._session.execute(
            select(RemyContextSource).where(
                RemyContextSource.organisation_id == org_id,
                RemyContextSource.user_id == user_id,
            )
        )
        rows = list(result.scalars())
        for row in rows:
            await self._session.delete(row)
