"""Persistence adapter for deterministic System Maps discovery drafts.

The discovery service owns graph inference; this adapter owns translating that
result into the System Maps tables.  It intentionally does not use the legacy
LifecycleMap tables or lifecycle-map validation helpers.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.models.system_map import SystemMap
from modulo.db.repositories.system_map import SystemMapRepository

_DISCOVERY_NAMESPACE = uuid.UUID("b7d0a2bb-e57e-4f4b-a4af-a7c2f2a4d2e1")


class SystemMapNameConflictError(Exception):
    """A requested draft name is already used by a live system map."""


async def _unused_session_factory() -> AsyncGenerator[AsyncSession, None]:  # pragma: no cover
    """Satisfy the shared repository constructor for explicit-session methods."""
    yield AsyncSession()


class SystemMapDiscoveryRepository:
    """Persist discovery drafts in the System Maps tables."""

    def __init__(self, repository: SystemMapRepository | None = None) -> None:
        self.repository = repository or SystemMapRepository(session_factory=_unused_session_factory)

    async def get_or_create_draft(
        self,
        session: AsyncSession,
        *,
        organisation_id: uuid.UUID,
        account_id: uuid.UUID,
        name: str,
        description: str | None,
    ) -> SystemMap:
        result = await session.execute(
            select(SystemMap).where(
                SystemMap.organisation_id == organisation_id,
                SystemMap.name == name,
                SystemMap.deleted_at.is_(None),
            )
        )
        existing = result.scalar_one_or_none()
        if existing is not None:
            raise SystemMapNameConflictError(f"A system map named {name!r} already exists")
        return await self.repository.create_map(
            session,
            organisation_id=organisation_id,
            name=name,
            description=description,
            visibility="org",
            account_id=account_id,
        )

    async def save_discovery_revision(
        self,
        session: AsyncSession,
        *,
        organisation_id: uuid.UUID,
        map_id: uuid.UUID,
        content: dict[str, Any],
        updated_by: uuid.UUID,
    ) -> tuple[SystemMap, uuid.UUID, int]:
        system_map = await self.repository.get_map_by_id(session, organisation_id=organisation_id, map_id=map_id)
        if system_map is None:
            raise ValueError("system map discovery draft not found")

        existing_revisions = await self.repository.list_revisions(
            session,
            organisation_id=organisation_id,
            system_map_id=map_id,
            include_deleted=True,
        )
        fingerprint = str(content["discovery_fingerprint"])
        revision_id = uuid.uuid5(_DISCOVERY_NAMESPACE, f"{map_id}:{fingerprint}")
        existing_revision = next((item for item in existing_revisions if item.id == revision_id), None)
        if existing_revision is None:
            revision_number = max((item.revision_number for item in existing_revisions), default=0) + 1
            revision = await self.repository.create_revision(
                session,
                organisation_id=organisation_id,
                system_map_id=map_id,
                revision_number=revision_number,
                revision_id=revision_id,
                content_json=content,
            )
            for node in content.get("nodes", []):
                await self.repository.create_node(
                    session,
                    organisation_id=organisation_id,
                    revision_id=revision.id,
                    node_key=node["node_key"],
                    node_type=node["node_type"],
                    display_name=node["display_name"],
                    config_json=node.get("config_json", {}),
                    pipeline_id=_uuid_or_none(node.get("pipeline_id")),
                    connector_instance_id=_uuid_or_none(node.get("connector_instance_id")),
                    owner_team_id=_uuid_or_none(node.get("owner_team_id")),
                    position_x=node.get("position_x", 0),
                    position_y=node.get("position_y", 0),
                )
            for edge in content.get("relationships", []):
                await self.repository.create_relationship(
                    session,
                    organisation_id=organisation_id,
                    revision_id=revision.id,
                    source_node_key=edge["source_node_key"],
                    target_node_key=edge["target_node_key"],
                    relationship_type=edge["relationship_type"],
                    config_json=edge.get("config_json", {}),
                )
        else:
            revision = existing_revision
            revision_number = revision.revision_number

        system_map.updated_by = updated_by
        system_map.published_revision_id = None
        await session.flush()
        return system_map, revision_id, revision_number


def _uuid_or_none(value: Any) -> uuid.UUID | None:
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    return uuid.UUID(str(value))


__all__ = ["SystemMapDiscoveryRepository", "SystemMapNameConflictError"]
