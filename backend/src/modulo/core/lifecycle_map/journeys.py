"""Map-scoped journey reads (FAR-144).

Journeys are org-floor rows minted at create time from a run's work-item
refs (see ``modulo.db.lifecycle_refs``). A lifecycle map "owns" the journeys
whose latest-stage identity points at one of its stages, plus — until they
gain a stage identity — journeys whose (kind, ref) has already run through one
of the map's stage pipelines.

All functions assume the caller has set the RLS org context via set_rls_org()
and is inside an active transaction (the route layer wraps calls in
``async with session.begin():``). Runs data is org-floor — callers must gate
with the ``run.list`` permission, never ``lifecycle_map.list``.
"""

from __future__ import annotations

import base64
import json
import uuid
from datetime import datetime

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.lifecycle_refs import canonicalise_kind, canonicalise_ref
from modulo.db.models.journey import Journey
from modulo.db.models.lifecycle_map_stage import LifecycleMapStage
from modulo.db.models.run import Run

_DEFAULT_LIMIT = 50
_DEFAULT_RUN_HISTORY_LIMIT = 20


def encode_cursor(updated_at: datetime, journey_id: uuid.UUID) -> str:
    """Opaque keyset cursor over ``(updated_at, id)`` (list ordering)."""
    payload = json.dumps([updated_at.isoformat(), str(journey_id)])
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")


def decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    """Decode a keyset cursor; raises ``ValueError`` for malformed input."""
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii") + b"=" * (-len(cursor) % 4))
        ts, jid = json.loads(raw.decode("utf-8"))
        return datetime.fromisoformat(ts), uuid.UUID(jid)
    except (TypeError, ValueError):
        raise ValueError("invalid pagination cursor") from None


async def _stage_pipeline_ids(session: AsyncSession, map_id: uuid.UUID) -> set[uuid.UUID]:
    """Pipeline ids registered as stages of *map_id* (non-null junction rows)."""
    result = await session.execute(
        select(LifecycleMapStage.pipeline_id).where(
            LifecycleMapStage.map_id == map_id,
            LifecycleMapStage.pipeline_id.isnot(None),
        )
    )
    return {row for (row,) in result.all() if row is not None}


async def _referenced_ref_pairs(session: AsyncSession, pipeline_ids: set[uuid.UUID]) -> set[tuple[str, str]]:
    """Distinct canonical (kind, ref) pairs stamped on runs of *pipeline_ids*.

    JSONB containment (``work_item_refs @> [...]``) is Postgres-only, so the
    match is done portably: fetch the refs of the map's stage-pipeline runs and
    collect their (kind, ref) pairs in Python. Journey (kind, ref) columns are
    canonical, matching the canonical refs stamped on runs (FAR-142).
    """
    if not pipeline_ids:
        return set()
    result = await session.execute(
        select(Run.work_item_refs).where(
            Run.pipeline_id.in_(pipeline_ids),
            Run.work_item_refs.isnot(None),
        )
    )
    pairs: set[tuple[str, str]] = set()
    for (refs,) in result.all():
        for entry in refs or []:
            if isinstance(entry, dict) and entry.get("kind") and entry.get("ref"):
                pairs.add((str(entry["kind"]), str(entry["ref"])))
    return pairs


async def list_map_journeys(
    session: AsyncSession,
    *,
    map_id: uuid.UUID,
    kind: str | None = None,
    ref: str | None = None,
    owner_team_id: uuid.UUID | None = None,
    cursor: str | None = None,
    limit: int = _DEFAULT_LIMIT,
) -> tuple[list[Journey], str | None]:
    """Map-scoped journeys ordered by ``updated_at DESC, id DESC``.

    Returns ``(journeys, next_cursor)``; ``next_cursor`` is ``None`` on the
    last page. A journey is map-scoped when:

    * its latest-stage identity points at this map (``map_id`` matches), or
    * it has no stage identity yet and at least one run through this map's
      stage pipelines stamped its (kind, ref).

    ``kind`` / ``ref`` narrow to a single exact journey (the map renderer's
    one-journey lookup). ``owner_team_id`` is applied by the caller for
    team-scoped maps.
    """
    if kind is not None:
        kind = canonicalise_kind(kind)
        if ref is not None:
            ref = canonicalise_ref(kind, ref)
    elif ref is not None:
        ref = ref.strip()

    stage_pipeline_ids = await _stage_pipeline_ids(session, map_id)
    referenced = await _referenced_ref_pairs(session, stage_pipeline_ids)

    conditions = [Journey.map_id == map_id]
    if referenced:
        conditions.append(
            and_(
                Journey.map_id.is_(None),
                Journey.stage_id.is_(None),
                or_(*[and_(Journey.kind == k, Journey.ref == r) for k, r in referenced]),
            )
        )

    query = select(Journey).where(or_(*conditions))
    if kind is not None:
        query = query.where(Journey.kind == kind)
    if ref is not None:
        query = query.where(Journey.ref == ref)
    if owner_team_id is not None:
        query = query.where(Journey.owner_team_id == owner_team_id)

    if cursor is not None:
        updated_at, journey_id = decode_cursor(cursor)
        query = query.where(
            or_(
                Journey.updated_at < updated_at,
                and_(Journey.updated_at == updated_at, Journey.id < journey_id),
            )
        )

    rows = list(
        (await session.execute(query.order_by(Journey.updated_at.desc(), Journey.id.desc()).limit(limit + 1))).scalars()
    )
    has_more = len(rows) > limit
    items = rows[:limit]
    next_cursor: str | None = None
    if has_more and items:
        last = items[-1]
        next_cursor = encode_cursor(last.updated_at, last.id)
    return items, next_cursor


async def get_map_journey(
    session: AsyncSession,
    *,
    map_id: uuid.UUID,
    kind: str,
    ref: str,
    owner_team_id: uuid.UUID | None = None,
) -> Journey | None:
    """Single journey detail by exact (kind, ref), scoped to *map_id*."""
    kind = canonicalise_kind(kind)
    ref = canonicalise_ref(kind, ref)

    stage_pipeline_ids = await _stage_pipeline_ids(session, map_id)
    referenced = await _referenced_ref_pairs(session, stage_pipeline_ids)

    conditions = [Journey.map_id == map_id]
    if (kind, ref) in referenced:
        conditions.append(
            and_(
                Journey.map_id.is_(None),
                Journey.stage_id.is_(None),
            )
        )

    query = select(Journey).where(
        or_(*conditions),
        Journey.kind == kind,
        Journey.ref == ref,
    )
    if owner_team_id is not None:
        query = query.where(Journey.owner_team_id == owner_team_id)
    return (await session.execute(query)).scalar_one_or_none()


async def list_journey_runs(
    session: AsyncSession,
    *,
    journey: Journey,
    limit: int = _DEFAULT_RUN_HISTORY_LIMIT,
) -> list[Run]:
    """Recent runs touching *journey*, most recent first (best-effort).

    A run touches the journey when its ``work_item_refs`` carries the journey's
    canonical (kind, ref) or its ``work_item_id`` equals the journey's canonical
    id. JSONB containment is Postgres-only, so the refs match is done in Python
    over the refs-carrying runs ordered by ``completed_at DESC``; the scan stops
    once *limit* matches are collected. Runs may be purged — an empty result is
    a valid "history lost to retention" outcome, not an error.
    """
    result = await session.execute(
        select(Run).where(Run.work_item_refs.isnot(None)).order_by(Run.completed_at.desc().nulls_last())
    )
    matched: list[Run] = []
    for run in result.scalars():
        if run.work_item_id == journey.canonical_work_item_id:
            matched.append(run)
        else:
            for entry in run.work_item_refs or []:
                if isinstance(entry, dict) and entry.get("kind") == journey.kind and entry.get("ref") == journey.ref:
                    matched.append(run)
                    break
        if len(matched) >= limit:
            break
    return matched
