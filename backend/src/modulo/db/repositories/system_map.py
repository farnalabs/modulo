"""Compatibility persistence for System Maps discovery.

FAR-2236 is not present on this branch's origin/main yet. Until its schema
lands, discovery drafts are stored in the existing organisation-scoped
LifecycleMap content document. The adapter keeps the discovery route isolated
from that compatibility detail and uses the same transaction/RLS boundary.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.models.lifecycle_map import LifecycleMap

_DISCOVERY_NAMESPACE = uuid.UUID("b7d0a2bb-e57e-4f4b-a4af-a7c2f2a4d2e1")


class SystemMapRepository:
    """Persist discovery drafts using the current pre-System-Maps contract."""

    async def get_or_create_draft(
        self,
        session: AsyncSession,
        *,
        organisation_id: uuid.UUID,
        account_id: uuid.UUID,
        name: str,
        description: str | None,
    ) -> LifecycleMap:
        result = await session.execute(
            select(LifecycleMap).where(
                LifecycleMap.organisation_id == organisation_id,
                LifecycleMap.name == name,
                LifecycleMap.archived_at.is_(None),
                LifecycleMap.deleted_at.is_(None),
            )
        )
        existing = result.scalar_one_or_none()
        if existing is not None:
            return existing
        map_obj = LifecycleMap(
            organisation_id=organisation_id,
            name=name,
            account_id=account_id,
            updated_by=account_id,
            description=description,
            visibility="org",
            version=1,
            content_json={
                "stages": [],
                "edges": [],
                "notes": "System Maps discovery draft; never published automatically.",
            },
        )
        session.add(map_obj)
        await session.flush()
        return map_obj

    async def save_discovery_revision(
        self,
        session: AsyncSession,
        *,
        organisation_id: uuid.UUID,
        map_id: uuid.UUID,
        content: dict[str, Any],
        updated_by: uuid.UUID,
    ) -> tuple[LifecycleMap, uuid.UUID]:
        result = await session.execute(
            select(LifecycleMap).where(
                LifecycleMap.id == map_id,
                LifecycleMap.organisation_id == organisation_id,
                LifecycleMap.archived_at.is_(None),
                LifecycleMap.deleted_at.is_(None),
            )
        )
        lifecycle_map = result.scalar_one_or_none()
        if lifecycle_map is None:
            raise ValueError("discovery draft map not found")
        revision_id = uuid.uuid5(_DISCOVERY_NAMESPACE, f"{map_id}:{content['discovery_fingerprint']}")
        content = dict(content)
        content["discovery_revision_id"] = str(revision_id)
        lifecycle_map.content_json = content
        lifecycle_map.version += 1
        lifecycle_map.updated_by = updated_by
        await session.flush()
        return lifecycle_map, revision_id
