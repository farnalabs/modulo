"""Journey advancement on run finalise (FAR-143 part 2).

The create-time path (``modulo.db.crud.run._hydrate_journeys``) MINTs journey
rows via ``INSERT ... ON CONFLICT DO NOTHING``. This module is the finalise
counterpart: when a run reaches a terminal (or ``awaiting_human``) state, it
advances the ``journeys`` rows for every canonical work-item ref the run
carried.

Advancement semantics per ref entry:

* A ref whose ``(organisation_id, kind, ref)`` row does not exist is UPSERTed
  into existence (the INSERT arm of ``ON CONFLICT``), so a run that is the
  first to touch a work item still records its evidence.
* ``run_count`` increments by 1 for TERMINAL advancing statuses
  (``complete`` / ``failed`` / ``eval_failed``). ``awaiting_human`` updates
  the latest evidence but does NOT count (the run is not terminal yet — it may
  later complete and would otherwise be counted twice for one journey cycle).
* Latest evidence (``latest_terminal_run_id``, ``latest_status``,
  ``latest_provenance``) is COMPARE-AND-SET: it only overwrites when the new
  run's evidence timestamp is strictly newer than the row's current evidence
  timestamp. Equal timestamps keep the existing evidence (deterministic
  first-writer-wins) — see the anchor note below.
* NON-ADVANCING runs (``cancelled`` / ``stalled``, ``is_replay=True``, or a
  ``variant_group_id``) never touch evidence or ``run_count``; the row is
  only ensured to exist (mint-only ``DO NOTHING``).
* Stage identity columns (``map_id`` / ``map_version`` / ``stage_id`` /
  ``stage_name`` / ``position``) are set ONLY when the run's pipeline is a
  lifecycle-map stage (resolved org-scoped via ``lifecycle_map_stages``).
  For a pipeline that is not a map stage, the stage columns are left
  untouched — a run on a non-map pipeline updates the latest evidence but does
  not move the journey's stage.
* An ``explicit_stage`` row may be supplied by the caller (workflow
  self-reports that name the completed stage via ``stage_id``) so journeys
  advance into EXTERNAL stages — GitHub Actions workflows (merge queue,
  deploy agent) that have no ``pipeline_id`` and therefore cannot be resolved
  by the pipeline path. The explicit stage is used only when pipeline
  resolution yields no stage (``pipeline_id`` is ``None`` or the pipeline is
  not a map stage); a pipeline-resolved stage always takes precedence.

Evidence-timestamp anchor (DEV IATION from the FAR-143 spec)
------------------------------------------------------------
The FAR-143 spec describes a ``journeys.latest_completed_at`` column as the
compare-and-set anchor. That column does not exist in the current schema
(model ``journeys`` + migration 0084): the table carries no persisted
completion timestamp. ``updated_at`` is used instead — every winning advance
stores its evidence timestamp there, so ``:evidence_ts > journeys.updated_at``
is exactly the "newer completed_at overwrites" rule. The spec's secondary
tie-break (``created_at`` then run id) cannot be expressed without a persisted
evidence ``created_at``; equal evidence timestamps therefore keep the existing
evidence (deterministic, no flapping). ``run_created_at`` is still accepted and
used as the evidence timestamp when ``completed_at`` is ``None`` (the
``awaiting_human`` case, where the run is not terminal and has no
``completed_at`` yet).

RLS / transaction contract
--------------------------
The caller owns the session: it MUST be inside an active transaction with the
org context set (``set_rls_org`` on Postgres; the ORM tenant filter on generic
backends) before calling. This module never calls ``set_rls_org`` — it only
runs queries against the session. On Postgres, RLS scopes all reads/writes to
the caller's organisation; on generic backends, the ORM stage lookup carries an
explicit ``organisation_id`` filter. All SQL is parameterised ``text()`` — no
string interpolation (repo rule).

Ref canonicalisation
--------------------
Each raw entry is run through ``validate_ref_entry`` (``modulo.db.lifecycle_refs``),
which canonicalises ``kind`` + ``ref`` (e.g. ``#123`` vs ``123`` for a github
kind both land on ``123``) and validates ``source`` / ``status``. A malformed
entry is dropped with a warning (fail-open), mirroring the create-time path.
Duplicates of the same canonical ``(kind, ref)`` within one call are collapsed
so a single run never double-counts the same journey.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, bindparam, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.runtime_config.mint_budget import consume_agent_mint_budget
from modulo.core.runtime_config.org_flags import FLAG_WORK_ITEM_AGENT_MINTING_ENABLED, is_org_flag_enabled
from modulo.db.lifecycle_refs import (
    REFS_EVENT_DISMISSAL_SUPPRESSED,
    canonical_work_item_id,
    notify_refs_event,
    validate_ref_entry,
)
from modulo.db.models.journey import Journey
from modulo.db.models.lifecycle_map_stage import LifecycleMapStage

_log = logging.getLogger(__name__)

# Terminal statuses that ADVANCE a journey (evidence + run_count). Cancelled
# and stalled are deliberately excluded — they mean "the work did not happen".
_ADVANCING_TERMINAL_STATUSES: frozenset[str] = frozenset(
    {
        "complete",
        "failed",
        "eval_failed",
        "compensation_failed",
        "router_no_match",
    }
)

_AWAITING_HUMAN = "awaiting_human"

# Mint-only: ensure the journey row exists without touching latest_*/run_count.
# Mirrors modulo.db.crud.run._hydrate_journeys (uuid bindings as 32-hex for
# cross-backend portability). The provenance / first_seen_source columns are
# stamped on the INSERT arm (FAR-794 + FAR-795): an agent-sourced mint must
# carry ``provenance='agent'`` / ``first_seen_source='agent'`` from birth, and
# caller/derived mint-only rows keep their rank-guarded stamp too. The DO
# NOTHING conflict arm never rewrites an existing row (``first_seen_source``
# stays immutable).
_MINT_SQL = text(
    "INSERT INTO journeys "
    "(id, organisation_id, kind, ref, canonical_work_item_id, provenance, first_seen_source, created_at, updated_at) "
    "VALUES (:id, :org_id, :kind, :ref, :canonical_id, :provenance, :first_seen_source, "
    "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP) "
    "ON CONFLICT (organisation_id, kind, ref) DO NOTHING"
)

# Advancing UPSERT. The evidence/run_count logic is per-row atomic:
#   * run_count always increments by :run_count_delta on the conflict arm;
#   * latest_* only overwrite when :evidence_ts > updated_at (compare-and-set);
#   * stage identity columns only change when a map-stage pipeline resolved
#     (:map_id IS NOT NULL); otherwise they are preserved unchanged.
# Qualified ``journeys.<col>`` references are REQUIRED in the DO UPDATE arm
# (FAR-622): bare column names are ambiguous there — Postgres 17 rejects the
# statement with ``column reference "updated_at" is ambiguous`` (asyncpg
# ``AmbiguousColumnError``; validated empirically against the production
# engine, Postgres 17.7 on flyio/postgres-flex) — so every EXISTING-row column
# inside the SET expressions must be table-qualified. The SET target columns on
# the left of ``=`` stay bare per SQL syntax, and proposed values live under
# ``excluded``. The qualified form is also valid on SQLite (table-qualified
# refs resolve to the existing row in its upsert DO UPDATE arm).
_ADVANCE_SQL = text(
    "INSERT INTO journeys ("
    "id, organisation_id, kind, ref, canonical_work_item_id, "
    "provenance, first_seen_source, "
    "latest_terminal_run_id, latest_status, latest_provenance, "
    'map_id, map_version, stage_id, stage_name, "position", '
    "run_count, created_at, updated_at"
    ") VALUES ("
    ":id, :org_id, :kind, :ref, :canonical_id, "
    ":provenance, :first_seen_source, "
    ":run_id, :status, :provenance, "
    ":map_id, :map_version, :stage_id, :stage_name, :position, "
    ":run_count_delta, CURRENT_TIMESTAMP, :evidence_ts"
    ") ON CONFLICT (organisation_id, kind, ref) DO UPDATE SET "
    "latest_terminal_run_id = CASE "
    "  WHEN (:evidence_ts > journeys.updated_at OR journeys.updated_at IS NULL) AND :run_id IS NOT NULL THEN :run_id "
    "  ELSE journeys.latest_terminal_run_id END, "
    "latest_status = CASE "
    "  WHEN :evidence_ts > journeys.updated_at OR journeys.updated_at IS NULL THEN :status "
    "  ELSE journeys.latest_status END, "
    "latest_provenance = CASE "
    "  WHEN :evidence_ts > journeys.updated_at OR journeys.updated_at IS NULL THEN :provenance "
    "  ELSE journeys.latest_provenance END, "
    "map_id = CASE WHEN (:evidence_ts > journeys.updated_at OR journeys.updated_at IS NULL) "
    "  AND :map_id IS NOT NULL THEN :map_id ELSE journeys.map_id END, "
    "map_version = CASE WHEN (:evidence_ts > journeys.updated_at OR journeys.updated_at IS NULL) "
    "  AND :map_id IS NOT NULL THEN :map_version ELSE journeys.map_version END, "
    "stage_id = CASE WHEN (:evidence_ts > journeys.updated_at OR journeys.updated_at IS NULL) "
    "  AND :map_id IS NOT NULL THEN :stage_id ELSE journeys.stage_id END, "
    "stage_name = CASE WHEN (:evidence_ts > journeys.updated_at OR journeys.updated_at IS NULL) "
    "  AND :map_id IS NOT NULL THEN :stage_name ELSE journeys.stage_name END, "
    '"position" = CASE WHEN (:evidence_ts > journeys.updated_at OR journeys.updated_at IS NULL) '
    '  AND :map_id IS NOT NULL THEN :position ELSE journeys."position" END, '
    "run_count = COALESCE(journeys.run_count, 0) + :run_count_delta, "
    "updated_at = CASE "
    "  WHEN :evidence_ts > journeys.updated_at OR journeys.updated_at IS NULL THEN :evidence_ts "
    "  ELSE journeys.updated_at END "
    # FAR-795 slice C — dismissal suppression at the upsert predicate: a
    # tombstone (``dismissed_at`` set) is never re-advanced, re-minted, or
    # re-stamped by ANY source on the mint chokepoint, no matter which path
    # carried the ref. This is the authoritative gate (not a TOCTOU read).
    "WHERE journeys.dismissed_at IS NULL"
).bindparams(
    # FAR-665: declare the :evidence_ts bind as a tz-aware DateTime so
    # SQLAlchemy converts per backend. On Postgres the parameter is inferred
    # as timestamptz (from the ``:evidence_ts > journeys.updated_at``
    # comparison), and asyncpg refuses to encode a Python str into it —
    # binding the ISO string raised ``asyncpg.DataError: invalid input for
    # query argument`` on EVERY advance, so journeys never advanced in
    # production. A datetime encodes natively. On SQLite the DateTime bind
    # processor renders exactly the storage format the ORM uses for
    # ``journeys.updated_at``, keeping the like-for-like string comparison.
    bindparam("evidence_ts", type_=DateTime(timezone=True)),
)

# FAR-794 slice 2b: the rank-guarded provenance UPSERT for caller/derived refs
# at terminal finalise — the same shape ``modulo.db.crud.run._hydrate_journeys``
# established at create time, applied to the mint entries that were absent at
# run creation (node-input injection arrived after the snapshot seeded input).
#
# * INSERT arm MINTS the missing caller/derived journey row (agents never mint).
# * UPDATE arm upgrades ``provenance`` by rank ONLY (``agent(0) < derived(1) <
#   caller(2)``, unknown/NULL legacy rank 0 — the same inline CASE the create
#   path in ``modulo.db.crud.run`` established) — an INDEPENDENT SET, never
#   gated by the ``:evidence_ts`` evidence compare-and-set, and it does
#   NOT touch ``updated_at`` / ``latest_*`` / ``run_count`` (those are owned
#   by terminal-advance evidence-CAS). A provenance upgrade never downgrades.
# * ``first_seen_source`` is immutable post-mint — the UPDATE arm never writes it.
_PROVENANCE_UPSERT_SQL = text(
    "INSERT INTO journeys "
    "(id, organisation_id, kind, ref, canonical_work_item_id, provenance, "
    "first_seen_source, created_at, updated_at) "
    "VALUES (:id, :org_id, :kind, :ref, :canonical_id, :provenance, "
    ":first_seen_source, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP) "
    "ON CONFLICT (organisation_id, kind, ref) DO UPDATE SET "
    "provenance = CASE "
    "WHEN (CASE :provenance WHEN 'caller' THEN 2 WHEN 'derived' THEN 1 WHEN 'agent' THEN 0 ELSE 0 END) "
    "> (CASE journeys.provenance WHEN 'caller' THEN 2 WHEN 'derived' THEN 1 WHEN 'agent' THEN 0 ELSE 0 END) "
    "THEN :provenance ELSE journeys.provenance END "
    # FAR-795 slice C: a dismissed (tombstone) row is never re-stamped.
    "WHERE journeys.dismissed_at IS NULL"
)


async def agent_minting_enabled(session: AsyncSession, organisation_id: uuid.UUID) -> bool:
    """Fail-closed org-flag gate for agent-sourced journey minting (FAR-795).

    True only when ``FLAG_WORK_ITEM_AGENT_MINTING_ENABLED`` is explicitly
    json-``True`` on the org; ANY read error means OFF (the underlying
    :func:`is_org_flag_enabled` is fail-closed). Every mint path that can
    observe an agent-sourced ref must consult this before minting.
    """
    return await is_org_flag_enabled(session, organisation_id, FLAG_WORK_ITEM_AGENT_MINTING_ENABLED)


def _persisted_provenance(source: str | None) -> str:
    """The provenance value that may be PERSISTED on a journey row (FAR-794).

    The stored ``latest_provenance`` vocabulary is invariant: only
    ``caller`` / ``derived`` / ``agent`` are ever written. The legacy
    ``reported`` marker stays accepted as the self-report confirm gate's
    INPUT (it marks an advisory operator claim) but is normalised to
    ``agent`` at the point the advance statement binds it. Unknown/legacy
    values also map to ``agent`` (rank 0), matching the create-path
    treatment in ``_PROVENANCE_UPSERT_SQL``.
    """
    return source if source in ("caller", "derived", "agent") else "agent"


def _mintable_source(entry: dict[str, Any]) -> bool:
    """True for the mintable ref sources (``caller`` / ``derived``).

    Agent-emitted refs are stored on the run and CONFIRMED against existing
    journey rows but never mint a row and never upgrade provenance — unless
    the org-scoped agent-minting flag (FAR-795) is enabled, which the caller
    gates explicitly.
    """
    return entry.get("source") in ("caller", "derived")


def _agent_source(entry: dict[str, Any]) -> bool:
    """True for agent-sourced entries (rank 0 in the provenance order)."""
    return entry.get("source") == "agent"


def _canonicalise_entry(entry: Any) -> dict[str, Any] | None:
    """Canonicalise + validate a raw work-item ref entry (fail-open).

    Returns the canonical ``{kind, ref, source, status?}`` entry, or ``None``
    when the entry is malformed (mirrors the create-time drop-with-warning).
    """
    try:
        return validate_ref_entry(entry)
    except (ValueError, TypeError) as exc:
        _log.warning("advance_journeys: dropping invalid work-item ref entry: %s", exc)
        return None


async def _resolve_stage_identity(
    session: AsyncSession,
    organisation_id: uuid.UUID,
    pipeline_id: uuid.UUID,
) -> LifecycleMapStage | None:
    """Resolve the lifecycle-map stage bound to *pipeline_id* (org-scoped).

    Returns ``None`` when the pipeline is not a map-stage pipeline (no active
    ``lifecycle_map_stages`` row). The partial unique index
    ``uq_lifecycle_map_stages_active_pipeline`` guarantees at most one row per
    (org, pipeline), so ``scalar_one_or_none`` is safe.
    """
    result = await session.execute(
        select(LifecycleMapStage).where(
            LifecycleMapStage.organisation_id == organisation_id,
            LifecycleMapStage.pipeline_id == pipeline_id,
        )
    )
    return result.scalar_one_or_none()


def _evidence_timestamp(completed_at: datetime | None, run_created_at: datetime) -> datetime:
    """Compare-and-set anchor: the run's ``completed_at``, falling back to its
    ``created_at`` when the run is not terminal (``awaiting_human``).

    Always returned as a tz-aware UTC ``datetime``. ``_ADVANCE_SQL`` declares
    the ``:evidence_ts`` bind as ``DateTime(timezone=True)`` (FAR-665): asyncpg
    infers the parameter as ``timestamptz`` and rejects a Python str with
    ``DataError``, so the value MUST be a real datetime on Postgres; the
    SQLite bind processor converts it to the same storage format the ORM uses
    for ``journeys.updated_at``, so the ``:evidence_ts > updated_at``
    compare-and-set stays like-for-like on generic backends (binding a raw
    datetime through an untyped ``text()`` would instead route through the
    sqlite3 default datetime adapter, which is deprecated on Python 3.12 —
    the declared bindparam type avoids that path).

    Naive datetimes (SQLite test seeds) are interpreted as UTC wall-clock;
    aware datetimes are normalised to UTC.
    """
    anchor = completed_at if completed_at is not None else run_created_at
    if anchor.tzinfo is None:
        return anchor.replace(tzinfo=UTC)
    return anchor.astimezone(UTC)


async def confirm_reported_refs(
    session: AsyncSession,
    organisation_id: uuid.UUID,
    entries: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """Confirm which reported entries match an existing journey row.

    Self-report is ADVISORY (FAR-143 spec v6): a reported claim can only
    CONFIRM / MATCH an existing journey keyed by the same canonical
    ``(organisation_id, kind, ref)`` — it can NEVER mint one (minting is owned
    by the create-time ``INSERT ... ON CONFLICT DO NOTHING`` path in
    ``modulo.db.crud.run``). This is the self-report endpoint's counterpart to
    the finalise path's ``_confirm_reported_refs``, kept here so both consumers
    share one org-scoped EXISTS check.

    ``entries`` must already be canonicalised by
    :func:`validate_and_normalise_reported_refs` (kind/ref canonical,
    ``source="reported"``). The legacy ``reported`` marker is accepted as the
    confirm gate's INPUT — the match keys on ``(org, kind, ref)`` only — but
    it is never PERSISTED: the advance write normalises it to ``agent``
    (``_persisted_provenance``), so ``journeys.latest_provenance`` only ever
    stores ``caller`` / ``derived`` / ``agent``. Returns
    ``(confirmed_entries, unmatched_count)``. The caller owns the RLS org
    context and an active transaction.
    """
    confirmed: list[dict[str, Any]] = []
    unmatched = 0
    for entry in entries:
        exists = (
            await session.execute(
                select(Journey.id).where(
                    Journey.organisation_id == organisation_id,
                    Journey.kind == entry["kind"],
                    Journey.ref == entry["ref"],
                    # FAR-795 slice C: a dismissed journey is treated as
                    # NONEXISTENT for every existence check — a citation of a
                    # dismissed work item cannot resurrect it.
                    Journey.dismissed_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if exists is not None:
            confirmed.append(entry)
        else:
            unmatched += 1
    return confirmed, unmatched


def _canonicalise_and_dedupe(refs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Canonicalise raw refs in order, dropping invalid and duplicate (kind, ref)."""
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for entry in refs:
        canonical = _canonicalise_entry(entry)
        if canonical is None:
            continue
        key = (canonical["kind"], canonical["ref"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(canonical)
    return deduped


def _ref_params(organisation_id: uuid.UUID, canonical: dict[str, Any]) -> dict[str, Any]:
    """The shared mint/advance statement params for one canonical ref."""
    return {
        "id": uuid.uuid4().hex,
        "org_id": organisation_id.hex,
        "kind": canonical["kind"],
        "ref": canonical["ref"],
        "canonical_id": canonical_work_item_id(organisation_id, canonical["kind"], canonical["ref"]).hex,
    }


async def _advance_stage_identity(
    session: AsyncSession,
    organisation_id: uuid.UUID,
    *,
    advancing: bool,
    pipeline_id: uuid.UUID | None,
    explicit_stage: LifecycleMapStage | None,
) -> LifecycleMapStage | None:
    """Resolve the lifecycle-map stage row; pipeline-resolved stage wins."""
    if not advancing:
        return None
    stage: LifecycleMapStage | None = None
    if pipeline_id is not None:
        stage = await _resolve_stage_identity(session, organisation_id, pipeline_id)
    if stage is None and explicit_stage is not None:
        stage = explicit_stage
    return stage


async def _mint_or_advance_ref(
    session: AsyncSession,
    *,
    organisation_id: uuid.UUID,
    canonical: dict[str, Any],
    advancing: bool,
    status: str,
    run_id: uuid.UUID | None,
    stage: LifecycleMapStage | None,
    run_count_delta: int,
    evidence_ts: datetime,
) -> int:
    """Execute the mint (non-advancing) or advance statement for one ref.

    Returns 0 for the mint path, 1 when a journey advanced.
    """
    provenance = _persisted_provenance(canonical.get("source"))
    params = _ref_params(organisation_id, canonical)
    params["first_seen_source"] = provenance
    if not advancing:
        params["provenance"] = provenance
        await session.execute(_MINT_SQL, params)
        return 0

    result = await session.execute(
        _ADVANCE_SQL,
        {
            **params,
            "run_id": run_id.hex if run_id is not None else None,
            "status": status,
            # FAR-794 persisted-source invariant: ``latest_provenance`` only
            # ever stores caller/derived/agent — a legacy ``reported`` input
            # marker is normalised here, at the write.
            "provenance": provenance,
            "map_id": stage.map_id.hex if stage is not None else None,
            "map_version": stage.version if stage is not None else None,
            "stage_id": stage.stage_id if stage is not None else None,
            "stage_name": stage.stage_name if stage is not None else None,
            "position": stage.position if stage is not None else None,
            "run_count_delta": run_count_delta,
            "evidence_ts": evidence_ts,
        },
    )
    # FAR-795 slice C: the advance statement carries a dismissal predicate
    # (``WHERE journeys.dismissed_at IS NULL``) — an arm that refused the
    # write (tombstone row) reports rowcount 0 and must NOT count as "a
    # journey advanced" (the reconcile sweep's advanced counter and the
    # HOUSEKEEPING readout both consume this number).
    return int(getattr(result, "rowcount", 0) or 0)  # raw-SQL Result exposes rowcount at runtime


async def advance_journeys(
    session: AsyncSession,
    organisation_id: uuid.UUID,
    run_id: uuid.UUID | None,
    pipeline_id: uuid.UUID | None,
    refs: list[dict[str, Any]],
    status: str,
    completed_at: datetime | None,
    run_created_at: datetime,
    is_replay: bool = False,
    variant_group_id: uuid.UUID | None = None,
    explicit_stage: LifecycleMapStage | None = None,
) -> int:
    """Advance ``journeys`` rows for every canonical work-item ref of a run.

    Args:
        session: Async session inside an active transaction, org context set.
        organisation_id: The org owning the journeys (and the refs).
        run_id: The finalising run (recorded as ``latest_terminal_run_id`` on a
            winning advance). ``None`` for workflow self-report evidence that
            has no backing run — the existing ``latest_terminal_run_id`` is
            then preserved (never cleared).
        pipeline_id: The run's pipeline; used to resolve lifecycle-map stage
            identity (org-scoped). Non-map pipelines never move the stage.
            ``None`` skips the stage lookup entirely (self-report callers that
            cannot attribute a pipeline still update latest evidence).
        refs: Raw work-item ref entries ``{kind, ref, source?, status?}``.
        status: The run's status — terminal advancing (``complete`` /
            ``failed`` / ``eval_failed``) advances evidence + ``run_count``;
            ``awaiting_human`` advances evidence only; ``cancelled`` /
            ``stalled`` (and any replay/variant run) never advance.
        completed_at: The run's ``completed_at``; ``None`` for non-terminal
            runs (falls back to *run_created_at* as the evidence anchor).
        run_created_at: The run's ``created_at``.
        is_replay: History-only replay run — never advances.
        variant_group_id: Variant run — never advances.
        explicit_stage: A pre-resolved lifecycle-map stage row supplied by the
            caller (external-stage self-reports that cannot attribute a
            ``pipeline_id``). The caller owns resolving it org-scoped and
            map-scoped against ``lifecycle_map_stages``. Used only when
            pipeline resolution yields no stage — a pipeline-resolved stage
            always takes precedence. ``None`` (the default) preserves the
            pipeline-only behaviour.

    Returns:
        The number of journeys advanced (evidence + possibly ``run_count``
        written). Mint-only non-advancing runs are not counted.

    Persisted provenance (FAR-794): the ``source`` carried by *refs* is
    accepted as the confirm/match input marker (legacy ``reported`` keeps
    matching), but the value written to ``journeys.latest_provenance`` is
    always normalised to ``caller`` / ``derived`` / ``agent`` — ``reported``
    is never persisted.

    """
    if not refs:
        return 0

    advancing = (
        (status in _ADVANCING_TERMINAL_STATUSES or status == _AWAITING_HUMAN)
        and not is_replay
        and variant_group_id is None
    )

    # run_count increments only for terminal advancing statuses; awaiting_human
    # updates latest evidence but must not count (the run is not terminal).
    run_count_delta = 1 if status in _ADVANCING_TERMINAL_STATUSES else 0
    evidence_ts = _evidence_timestamp(completed_at, run_created_at)

    stage = await _advance_stage_identity(
        session,
        organisation_id,
        advancing=advancing,
        pipeline_id=pipeline_id,
        explicit_stage=explicit_stage,
    )

    advanced = 0
    for canonical in _canonicalise_and_dedupe(refs):
        advanced += await _mint_or_advance_ref(
            session,
            organisation_id=organisation_id,
            canonical=canonical,
            advancing=advancing,
            status=status,
            run_id=run_id,
            stage=stage,
            run_count_delta=run_count_delta,
            evidence_ts=evidence_ts,
        )
    return advanced


async def upsert_ref_provenances(
    session: AsyncSession,
    organisation_id: uuid.UUID,
    refs: list[dict[str, Any]],
    *,
    include_agent: bool = False,
) -> tuple[int, int, int]:
    """Rank-guarded provenance UPSERT for the mintable (caller/derived) refs.

    FAR-794 slice 2b: at terminal finalise the run's create-stamped refs are
    minted through the rank-guarded upsert (the same shape the create path in
    ``modulo.db.crud.run`` uses) — the UPDATE arm upgrades ``provenance`` ONLY
    by rank (derived → caller), independent of the evidence compare-and-set:
    a provenance upgrade is an independent SET that must never gate, or be
    gated by, the ``latest_*`` evidence write. ``first_seen_source`` is
    immutable (never rewritten here).

    Agent sourcing (FAR-795 slice B): agent-sourced entries mint ONLY when
    *include_agent* is True (the org flag already evaluated by the caller —
    fail-closed upstream). The INSERT arm stamps
    ``provenance='agent'`` / ``first_seen_source='agent'`` at first mint;
    rank 0 means an agent write can never downgrade or upgrade an existing
    row's provenance. When *include_agent* is False (flag OFF) agent entries
    are NOT written — but the ones whose journey row is MISSING (i.e. the
    mint that was suppressed) are counted, so the caller can record the
    ``refs_agent_mint_suppressed_by_flag`` counter without any write.

    Returns ``(considered, agent_minted, agent_suppressed)``:
    considered = caller/derived upserts attempted;
    agent_minted = agent-sourced INSERTs minted (only when include_agent);
    agent_suppressed = agent-sourced mints suppressed by the flag (only when
    not include_agent).

    Runs inside the caller's transaction (the journey savepoint at finalise);
    the caller owns the RLS org context.
    """
    considered = 0
    agent_minted = 0
    agent_suppressed = 0
    dismissed_suppressed = 0
    for entry in refs:
        mintable = _mintable_source(entry)
        agent = _agent_source(entry)
        if not mintable and not agent:
            continue
        try:
            canonical = validate_ref_entry(entry)
        except (ValueError, TypeError) as exc:
            _log.warning("upsert_ref_provenances: dropping invalid work-item ref entry: %s", exc)
            continue
        if canonical is None:
            continue
        if agent:
            rows = (
                await session.execute(
                    select(Journey.id, Journey.dismissed_at).where(
                        Journey.organisation_id == organisation_id,
                        Journey.kind == canonical["kind"],
                        Journey.ref == canonical["ref"],
                    )
                )
            ).first()
            # FAR-795 slice C: a DISMISSED tombstone row suppresses the mint
            # on any path — count it and skip the write entirely (the upsert
            # conservative gate; this probe adds the
            # counter). A caller citation never un-dismisses.
            if rows is not None and rows[1] is not None:
                dismissed_suppressed += 1
                continue
            if rows is None:
                # Row missing: the agent mint either happens (flag ON) or was
                # suppressed by the flag (flag OFF). An existing row's
                # agent-cited write is a rank-0 no-op — not counted.
                if include_agent:
                    # FAR-795 budget cap: a fresh agent mint must clear the
                    # per-org budget before it is written. consume_agent_mint_budget
                    # is fail-open (errors/guard-outage allow), so only a genuine
                    # within-window over-spend denies — and then the mint is
                    # suppressed exactly like the flag-OFF path.
                    from modulo.core.lifecycle_map.reconcile import (
                        REFS_EVENT_AGENT_MINT_BUDGET_EXCEEDED,
                    )

                    budget_ok = await consume_agent_mint_budget(session, organisation_id, n=1)
                    if not budget_ok:
                        notify_refs_event(REFS_EVENT_AGENT_MINT_BUDGET_EXCEEDED, count=1)
                        agent_suppressed += 1
                        continue
                    agent_minted += 1
                else:
                    agent_suppressed += 1
                    continue
            # An existing agent row mint: run the rank-0 upsert when allowed
            # (harmless no-op on the conflict arm); never when OFF.
            elif not include_agent:
                continue
        params = _ref_params(organisation_id, canonical)
        params.update(
            {
                "provenance": canonical["source"],
                "first_seen_source": canonical["source"],
            }
        )
        await session.execute(_PROVENANCE_UPSERT_SQL, params)
        considered += 1
    if dismissed_suppressed:
        notify_refs_event(REFS_EVENT_DISMISSAL_SUPPRESSED, count=dismissed_suppressed)
    return considered, agent_minted, agent_suppressed
