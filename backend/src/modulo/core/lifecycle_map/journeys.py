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
from datetime import UTC, datetime
from typing import cast

from sqlalchemy import ColumnElement, Select, String, and_, bindparam, or_, select, text
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


def _is_unattributed(journey: Journey, referenced: set[tuple[str, str]]) -> bool:
    """A journey is *unattributed* when a map stage pipeline has seen its work
    item but the journey never advanced into a map stage.

    True only when ALL of:
    * the latest stage identity is null (no ``map_id``/``stage_id``) — the
      journey was hydrated from a create-stamp, never advanced by
      ``advance_journeys``;
    * zero terminal runs (``run_count == 0``);
    * at least one surviving run through this map's stage pipelines stamped
      the journey's canonical ``(kind, ref)`` (the ``referenced`` set).

    The run-existence leg is what separates a genuine unattributed hole (the
    work item was hydrated from a create-stamp but the run never advanced to a
    map stage) from a brand-new journey that simply has not been run yet. The
    simpler ``no stage and no runs`` rule would false-positive on that second
    group, so we require a matching surviving run — the same set
    ``list_map_journeys`` already computes for orphan scoping, so this costs no
    extra query.
    """
    if journey.map_id is not None or journey.stage_id is not None:
        return False
    if (journey.run_count or 0) > 0:
        return False
    return (journey.kind, journey.ref) in referenced


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
    status: str | None = None,
    updated_since: datetime | None = None,
    cursor: str | None = None,
    limit: int = _DEFAULT_LIMIT,
) -> tuple[list[tuple[Journey, bool]], str | None]:
    """Map-scoped journeys ordered by ``updated_at DESC, id DESC``.

    Returns ``((journey, unattributed) pairs, next_cursor)``; ``next_cursor``
    is ``None`` on the last page. ``unattributed`` is True when a map stage
    pipeline has seen the journey's work item but it never advanced into a map
    stage (see ``_is_unattributed``). A journey is map-scoped when:

    * its latest-stage identity points at this map (``map_id`` matches), or
    * it has no stage identity yet and at least one run through this map's
      stage pipelines stamped its (kind, ref).

    ``kind`` / ``ref`` narrow to a single exact journey (the map renderer's
    one-journey lookup). ``owner_team_id`` is applied by the caller for
    team-scoped maps. ``status`` narrows to a ``latest_status`` value
    (``complete`` / ``failed``; validated by the route). ``updated_since``
    keeps only journeys whose ``updated_at`` (when the journey last moved) is
    at or after the given instant.
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

    query = select(Journey).where(or_(*conditions), Journey.dismissed_at.is_(None))
    if kind is not None:
        query = query.where(Journey.kind == kind)
    if ref is not None:
        query = query.where(Journey.ref == ref)
    if owner_team_id is not None:
        query = query.where(Journey.owner_team_id == owner_team_id)
    if status is not None:
        query = query.where(Journey.latest_status == status)
    if updated_since is not None:
        query = query.where(Journey.updated_at >= updated_since)

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
    return [(journey, _is_unattributed(journey, referenced)) for journey in items], next_cursor


async def get_map_journey(
    session: AsyncSession,
    *,
    map_id: uuid.UUID,
    kind: str,
    ref: str,
    owner_team_id: uuid.UUID | None = None,
) -> tuple[Journey, bool] | None:
    """Single journey detail by exact (kind, ref), scoped to *map_id*.

    Returns ``(journey, unattributed)`` or ``None`` when no journey matches;
    ``unattributed`` is True when a map stage pipeline has seen the journey's
    work item but it never advanced into a map stage (see
    ``_is_unattributed``).
    """
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
        # FAR-795 slice C: dismissed tombstones are invisible to the map UI.
        Journey.dismissed_at.is_(None),
    )
    if owner_team_id is not None:
        query = query.where(Journey.owner_team_id == owner_team_id)
    journey = (await session.execute(query)).scalar_one_or_none()
    if journey is None:
        return None
    return journey, _is_unattributed(journey, referenced)


def _is_postgres(session: AsyncSession) -> bool:
    """True when the session's bound engine speaks Postgres.

    Dialect-detected at query-build time (never a static import) so the
    SQLite unit-test path keeps the portable Python scan.
    """
    bind = session.sync_session.get_bind()
    return getattr(bind.dialect, "name", "") == "postgresql"


def _journey_refs_containment(journey: Journey) -> ColumnElement[bool]:
    """JSONB containment predicate for the journey's canonical (kind, ref).

    ``work_item_refs @> '[{"kind": K, "ref": R}]'::jsonb`` — the stored
    entries are ``{"kind", "ref", "source", status?}`` (validate_ref_entry
    shape), so a two-key containment template subset-matches EVERY entry shape
    ever persisted. The default jsonb GIN operator class on
    ``ix_runs_work_item_refs_gin`` (WHERE jsonb_array_length > 0) serves this
    operator; a containment-in-an-array implies a non-empty array, so the
    partial predicate is implied.

    The bound value is a JSON *string* bound as TEXT and cast to jsonb inside
    SQL. Binding it as a JSONB-typed parameter makes asyncpg double-encode the
    string into a jsonb scalar (``'"[{…}]"'``), so the containment match
    silently returns nothing; a parametrised ``CAST(:t AS jsonb)`` with a
    TEXT-typed bindparam keeps the on-wire value a plain JSON string that
    Postgres parses into the intended jsonb array.
    """
    template = json.dumps([{"kind": journey.kind, "ref": journey.ref}])
    return cast(
        ColumnElement[bool],
        text("work_item_refs @> CAST(:t AS jsonb)").bindparams(bindparam("t", template, type_=String)),
    )


def _journey_runs_postgres_query(journey: Journey, *, limit: int) -> Select[tuple[Run]]:
    """Scalable Postgres predicate walk for ``list_journey_runs``.

    Translates the portable scan exactly: only refs-carrying runs, matched by
    JSONB containment OR the canonical work-item-id anchor (when set), newest
    first, SQL-side ``LIMIT``.
    """
    conditions: list[ColumnElement[bool]] = [Run.work_item_refs.isnot(None)]
    if journey.canonical_work_item_id is not None:
        conditions.append(or_(_journey_refs_containment(journey), Run.work_item_id == journey.canonical_work_item_id))
    else:
        conditions.append(_journey_refs_containment(journey))
    return select(Run).where(*conditions).order_by(Run.completed_at.desc().nulls_last()).limit(limit)


async def list_journey_runs(
    session: AsyncSession,
    *,
    journey: Journey,
    limit: int = _DEFAULT_RUN_HISTORY_LIMIT,
) -> list[Run]:
    """Recent runs touching *journey*, most recent first (best-effort).

    A run touches the journey when its ``work_item_refs`` carries the journey's
    canonical (kind, ref) or its ``work_item_id`` equals the journey's canonical
    id. On Postgres the refs match is a JSONB containment predicate so the
    partial GIN index ``ix_runs_work_item_refs_gin`` bounds the scan to
    refs-carrying runs (an unbounded full-table walk on a user-facing path
    otherwise); non-Postgres dialects (SQLite unit tests) fall back to the
    portable Python scan with identical semantics. Runs may be purged — an
    empty result is a valid "history lost to retention" outcome, not an error.
    """
    if _is_postgres(session):
        result = await session.execute(_journey_runs_postgres_query(journey, limit=limit))
        return list(result.scalars())
    # Portable fallback (SQLite unit tests): unbounded walk, Python filter.
    result = await session.execute(
        select(Run).where(Run.work_item_refs.isnot(None)).order_by(Run.completed_at.desc().nulls_last())
    )
    matched: list[Run] = []
    for run in result.scalars():
        if _run_matches_journey(run, journey):
            matched.append(run)
        if len(matched) >= limit:
            break
    return matched


def _run_matches_journey(run: Run, journey: Journey) -> bool:
    """True when *run* touches *journey* under the portable (non-Postgres) scan.

    Mirrors the Postgres JSONB containment predicate: an exact canonical
    work-item-id anchor, or any ``work_item_refs`` entry carrying the journey's
    (kind, ref).
    """
    if run.work_item_id == journey.canonical_work_item_id:
        return True
    for entry in run.work_item_refs or []:
        if isinstance(entry, dict) and entry.get("kind") == journey.kind and entry.get("ref") == journey.ref:
            return True
    return False


async def _fetch_journey(
    session: AsyncSession,
    organisation_id: uuid.UUID,
    *,
    kind: str,
    ref: str,
    dismissed: bool | None,
) -> Journey | None:
    """Fetch the single journey for (org, kind, ref), optionally filtering on
    tombstone state.

    ``dismissed=True`` matches only dismissed (tombstoned) rows,
    ``dismissed=False`` matches only active rows, and ``dismissed=None`` ignores
    the tombstone filter. Kind/ref are canonicalised before the lookup.
    """
    kind = canonicalise_kind(kind)
    ref = canonicalise_ref(kind, ref)
    filters: list[ColumnElement[bool]] = [
        Journey.organisation_id == organisation_id,
        Journey.kind == kind,
        Journey.ref == ref,
    ]
    if dismissed is True:
        filters.append(Journey.dismissed_at.is_not(None))
    elif dismissed is False:
        filters.append(Journey.dismissed_at.is_(None))
    return (await session.execute(select(Journey).where(*filters))).scalar_one_or_none()


async def dismiss_journey(
    session: AsyncSession,
    organisation_id: uuid.UUID,
    *,
    kind: str,
    ref: str,
    dismissed_by: uuid.UUID,
    reason: str | None = None,
) -> bool:
    """Soft-dismiss (tombstone) the journey for (kind, ref) — operator action.

    Sets ``dismissed_at`` / ``dismissed_by`` / ``reason`` on the EXISTING row.
    A dismissed journey is invisible to every read path and is never re-minted
    or re-advanced (the upsert predicates refuse it). Returns False when no
    ACTIVE row matches (missing, or already dismissed — dismiss is idempotent
    and never overwrites an existing tombstone's reason).
    """
    journey = await _fetch_journey(session, organisation_id, kind=kind, ref=ref, dismissed=False)
    if journey is None:
        return False
    journey.dismissed_at = datetime.now(UTC)
    journey.dismissed_by = dismissed_by
    journey.reason = reason
    return True


async def restore_journey(
    session: AsyncSession,
    organisation_id: uuid.UUID,
    *,
    kind: str,
    ref: str,
) -> bool:
    """Clear a tombstone — operator restore (the ONLY un-dismissal path).

    Returns False when no DISMISSED row matches (missing, or already active —
    restore is idempotent). Restoring makes the journey writable again: the
    next mint/advance conflict arm passes the ``dismissed_at IS NULL``
    predicate as usual. Nothing else is reset — latest evidence and
    ``run_count`` are the finalise path's domain.
    """
    journey = await _fetch_journey(session, organisation_id, kind=kind, ref=ref, dismissed=True)
    if journey is None:
        return False
    journey.dismissed_at = None
    journey.dismissed_by = None
    journey.reason = None
    return True
