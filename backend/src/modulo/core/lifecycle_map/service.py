"""Org-scoped CRUD for LifecycleMap.

All functions assume the caller has set the RLS org context via set_rls_org()
before calling. The session must be within an active transaction.
"""

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.lifecycle_map.validation import (
    LifecycleMapContentError,
    LifecycleMapPipelineConflictError,
    normalize_content,
)
from modulo.db.crud.base import PageResult, apply_updates
from modulo.db.models.lifecycle_map import LifecycleMap
from modulo.db.models.lifecycle_map_stage import LifecycleMapStage

_log = logging.getLogger(__name__)


async def create_lifecycle_map(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    name: str,
    account_id: uuid.UUID,
    description: str | None = None,
    owner_team_id: uuid.UUID | None = None,
    visibility: str = "org",
    version: int = 1,
    content_json: dict[str, Any] | None = None,
) -> LifecycleMap:
    lifecycle_map = LifecycleMap(
        organisation_id=org_id,
        name=name,
        account_id=account_id,
        description=description,
        owner_team_id=owner_team_id,
        visibility=visibility,
        version=version,
        content_json=normalize_content(content_json),
    )
    session.add(lifecycle_map)
    await _check_pipeline_uniqueness(session, lifecycle_map)
    await session.flush()
    await derive_lifecycle_map_stages(session, lifecycle_map)
    return lifecycle_map


async def get_lifecycle_map(session: AsyncSession, lifecycle_map_id: uuid.UUID) -> LifecycleMap | None:
    result = await session.execute(
        select(LifecycleMap).where(
            LifecycleMap.id == lifecycle_map_id,
            LifecycleMap.archived_at.is_(None),
            LifecycleMap.deleted_at.is_(None),
        )
    )
    return result.scalar_one_or_none()


async def list_lifecycle_maps(
    session: AsyncSession,
    *,
    page: int = 1,
    page_size: int = 20,
    owner_team_id: uuid.UUID | None = None,
    include_archived: bool = False,
) -> PageResult[LifecycleMap]:
    offset = (page - 1) * page_size
    query = select(LifecycleMap).where(LifecycleMap.deleted_at.is_(None))
    count_query = select(func.count()).select_from(LifecycleMap).where(LifecycleMap.deleted_at.is_(None))
    if not include_archived:
        query = query.where(LifecycleMap.archived_at.is_(None))
        count_query = count_query.where(LifecycleMap.archived_at.is_(None))
    if owner_team_id is not None:
        query = query.where(LifecycleMap.owner_team_id == owner_team_id)
        count_query = count_query.where(LifecycleMap.owner_team_id == owner_team_id)
    try:
        total = (await session.execute(count_query)).scalar_one()
    except ProgrammingError:
        return PageResult(items=[], total=0, page=page, page_size=page_size)
    items = list(
        (
            await session.execute(query.order_by(LifecycleMap.updated_at.desc()).offset(offset).limit(page_size))
        ).scalars()
    )
    return PageResult(items=items, total=total, page=page, page_size=page_size)


async def update_lifecycle_map(
    session: AsyncSession,
    lifecycle_map_id: uuid.UUID,
    updates: dict[str, Any],
) -> LifecycleMap | None:
    lifecycle_map = await get_lifecycle_map(session, lifecycle_map_id)
    if lifecycle_map is None:
        return None
    if "content_json" in updates:
        updates = dict(updates)
        updates["content_json"] = normalize_content(updates["content_json"])
    apply_updates(lifecycle_map, updates)
    if "content_json" in updates:
        await _check_pipeline_uniqueness(session, lifecycle_map)
        await derive_lifecycle_map_stages(session, lifecycle_map)
    await session.flush()
    return lifecycle_map


async def delete_lifecycle_map(session: AsyncSession, lifecycle_map_id: uuid.UUID) -> bool:
    lifecycle_map = await get_lifecycle_map(session, lifecycle_map_id)
    if lifecycle_map is None:
        return False
    lifecycle_map.deleted_at = datetime.now(UTC)
    # Soft-deleting a map frees its pipelines for re-registration in another
    # map: the junction projection rows are removed with it.
    await session.execute(delete(LifecycleMapStage).where(LifecycleMapStage.map_id == lifecycle_map_id))
    await session.flush()
    return True


async def restore_lifecycle_map(session: AsyncSession, lifecycle_map_id: uuid.UUID) -> LifecycleMap | None:
    result = await session.execute(
        select(LifecycleMap).where(
            LifecycleMap.id == lifecycle_map_id,
            LifecycleMap.deleted_at.isnot(None),
        )
    )
    lifecycle_map = result.scalar_one_or_none()
    if lifecycle_map is None:
        return None
    lifecycle_map.deleted_at = None
    await derive_lifecycle_map_stages(session, lifecycle_map)
    await session.flush()
    return lifecycle_map


async def save_map_version(
    session: AsyncSession,
    lifecycle_map_id: uuid.UUID,
    *,
    stages: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    notes: str = "",
) -> LifecycleMap | None:
    """Save + publish a new active version of the map.

    Simplified v1 semantics: the payload replaces ``content_json``, the version
    counter bumps, and the junction projection is re-derived. No immutable
    version history is retained yet.
    """
    lifecycle_map = await get_lifecycle_map(session, lifecycle_map_id)
    if lifecycle_map is None:
        return None
    lifecycle_map.content_json = normalize_content({"stages": stages, "edges": edges, "notes": notes})
    lifecycle_map.version += 1
    await _check_pipeline_uniqueness(session, lifecycle_map)
    await derive_lifecycle_map_stages(session, lifecycle_map)
    await session.flush()
    return lifecycle_map


async def graduate_stage(
    session: AsyncSession,
    lifecycle_map_id: uuid.UUID,
    *,
    stage_id: str,
    pipeline_id: str | None,
) -> LifecycleMap | None:
    """Mark a journey/map-stage as graduated and link it to a Modulo pipeline.

    v1: records the graduation on the active version (sets ``graduated``,
    flips the stage ``type`` to ``modulo`` and links ``pipeline_id``). Full
    reclassification + history semantics are deferred to a later slice.
    """
    lifecycle_map = await get_lifecycle_map(session, lifecycle_map_id)
    if lifecycle_map is None:
        return None
    content: dict[str, Any] = lifecycle_map.content_json or {}
    stages = content.get("stages") if isinstance(content, dict) else None
    if not isinstance(stages, list):
        raise LifecycleMapContentError("map has no stages; nothing to graduate")
    target = next((s for s in stages if isinstance(s, dict) and s.get("id") == stage_id), None)
    if target is None:
        raise LifecycleMapContentError(f"stage {stage_id!r} not found in this map")
    target["graduated"] = True
    target["type"] = "modulo"
    target["pipeline_id"] = pipeline_id or None
    lifecycle_map.content_json = normalize_content(content)
    lifecycle_map.version += 1
    await _check_pipeline_uniqueness(session, lifecycle_map)
    await derive_lifecycle_map_stages(session, lifecycle_map)
    await session.flush()
    return lifecycle_map


async def _check_pipeline_uniqueness(session: AsyncSession, lifecycle_map: LifecycleMap) -> None:
    """Reject a save that would register a pipeline as a stage of two active maps.

    Junction rows of soft-deleted maps are removed on soft-delete, so any row
    found here belongs to an active map. The partial unique index
    ``uq_lifecycle_map_stages_active_pipeline`` is the DB-level backstop.
    """
    content = lifecycle_map.content_json if isinstance(lifecycle_map.content_json, dict) else {}
    pipeline_ids: set[uuid.UUID] = set()
    for stage in content.get("stages") or []:
        if not isinstance(stage, dict):
            continue
        raw = stage.get("pipeline_id")
        if not isinstance(raw, str) or not raw.strip():
            continue
        try:
            pipeline_ids.add(uuid.UUID(raw))
        except ValueError:
            continue
    if not pipeline_ids:
        return
    result = await session.execute(
        select(LifecycleMapStage.pipeline_id).where(
            LifecycleMapStage.pipeline_id.in_(pipeline_ids),
            LifecycleMapStage.map_id != lifecycle_map.id,
        )
    )
    taken = {row for (row,) in result.all() if row is not None}
    if taken:
        raise LifecycleMapPipelineConflictError(
            "pipeline(s) already a stage of another active lifecycle map: " + ", ".join(sorted(str(t) for t in taken))
        )


async def derive_lifecycle_map_stages(session: AsyncSession, lifecycle_map: LifecycleMap) -> None:
    """Replace the junction rows for *lifecycle_map* with rows derived from content_json.

    This is a derived projection (delete + re-insert for the active version).
    Shape-incompatible rows are skipped and logged, never fatal — content_json
    remains the source of truth.
    """
    await session.execute(delete(LifecycleMapStage).where(LifecycleMapStage.map_id == lifecycle_map.id))
    content = lifecycle_map.content_json if isinstance(lifecycle_map.content_json, dict) else {}
    stages = content.get("stages")
    if not isinstance(stages, list):
        return
    for position, stage in enumerate(stages):
        if not isinstance(stage, dict):
            continue
        pipeline_id: uuid.UUID | None = None
        pipeline_raw = stage.get("pipeline_id")
        if isinstance(pipeline_raw, str) and pipeline_raw.strip():
            try:
                pipeline_id = uuid.UUID(pipeline_raw)
            except ValueError:
                _log.warning(
                    "lifecycle_map_stages derive: non-UUID pipeline_id %r skipped for map %s",
                    pipeline_raw,
                    lifecycle_map.id,
                )
                continue
        session.add(
            LifecycleMapStage(
                organisation_id=lifecycle_map.organisation_id,
                account_id=lifecycle_map.account_id,
                map_id=lifecycle_map.id,
                version=lifecycle_map.version,
                stage_id=stage.get("id", ""),
                stage_name=stage.get("name", ""),
                position=position,
                stage_type=stage.get("type", "placeholder"),
                pipeline_id=pipeline_id,
            )
        )
    await session.flush()
