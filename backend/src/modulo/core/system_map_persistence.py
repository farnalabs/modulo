"""Compatibility persistence for System Maps discovery.

FAR-2236 is not present on this branch's origin/main yet. Until its schema
lands, discovery drafts are stored in the existing organisation-scoped
LifecycleMap content document. The adapter keeps the discovery route isolated
from that compatibility detail and uses the same transaction/RLS boundary.

This adapter lives in ``core`` (not ``db.repositories``) because it enforces
the shipped lifecycle-map WRITE invariants — row lock, pipeline uniqueness and
the derived stage projection — which the import-linter contract forbids the DB
layer from importing.

Discovery drafts are marked as discovery-owned in ``content_json`` so this
adapter can never adopt (and then overwrite) a user-authored map that happens
to share the requested name. A name collision with a non-discovery map is
reported as :class:`SystemMapNameConflictError` and surfaced as a 409.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.lifecycle_map.service import (
    _check_pipeline_uniqueness,
    derive_lifecycle_map_stages,
    get_lifecycle_map_for_update,
)
from modulo.db.models.lifecycle_map import LifecycleMap

_DISCOVERY_NAMESPACE = uuid.UUID("b7d0a2bb-e57e-4f4b-a4af-a7c2f2a4d2e1")

#: Marks a ``lifecycle_maps`` row as owned by the discovery adapter. A map
#: without this marker is user-authored and must never be adopted or rewritten.
_DISCOVERY_MARKER = "system_maps_discovery_draft"


class SystemMapNameConflictError(Exception):
    """A requested draft name is already used by a user-authored map."""


def _is_discovery_owned(lifecycle_map: LifecycleMap) -> bool:
    content = lifecycle_map.content_json
    return isinstance(content, dict) and content.get(_DISCOVERY_MARKER) is True


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
        # Two concurrent callers can both miss this name and both insert; the
        # ``(organisation_id, name)`` partial unique index then rejects the
        # loser as an IntegrityError (409). That race is acceptable for this
        # compatibility adapter: no caller can be handed a duplicate row, and
        # the retry path is the ordinary create flow.
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
            if not _is_discovery_owned(existing):
                raise SystemMapNameConflictError(
                    f"A lifecycle map named {name!r} already exists and is not a discovery draft"
                )
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
                _DISCOVERY_MARKER: True,
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
        # FOR UPDATE serialises concurrent saves so the version counter cannot
        # duplicate; the shipped lifecycle-map invariants then run exactly as
        # they do for every other content write.
        lifecycle_map = await get_lifecycle_map_for_update(session, map_id)
        if lifecycle_map is None or lifecycle_map.organisation_id != organisation_id:
            raise ValueError("discovery draft map not found")
        if not _is_discovery_owned(lifecycle_map):
            raise ValueError("lifecycle map is not a discovery draft")
        revision_id = uuid.uuid5(_DISCOVERY_NAMESPACE, f"{map_id}:{content['discovery_fingerprint']}")
        content = dict(content)
        content[_DISCOVERY_MARKER] = True
        content["discovery_revision_id"] = str(revision_id)
        lifecycle_map.content_json = content
        lifecycle_map.version += 1
        lifecycle_map.updated_by = updated_by
        await _check_pipeline_uniqueness(session, lifecycle_map)
        await derive_lifecycle_map_stages(session, lifecycle_map)
        await session.flush()
        return lifecycle_map, revision_id
