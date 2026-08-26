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
from datetime import datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.lifecycle_refs import canonical_work_item_id, validate_ref_entry
from modulo.db.models.journey import Journey
from modulo.db.models.lifecycle_map_stage import LifecycleMapStage

_log = logging.getLogger(__name__)

# Terminal statuses that ADVANCE a journey (evidence + run_count). Cancelled
# and stalled are deliberately excluded — they mean "the work did not happen".
_ADVANCING_TERMINAL_STATUSES: frozenset[str] = frozenset({"complete", "failed", "eval_failed", "router_no_match"})

_AWAITING_HUMAN = "awaiting_human"

# Mint-only: ensure the journey row exists without touching latest_*/run_count.
# Mirrors modulo.db.crud.run._hydrate_journeys (uuid bindings as 32-hex for
# cross-backend portability).
_MINT_SQL = text(
    "INSERT INTO journeys "
    "(id, organisation_id, kind, ref, canonical_work_item_id, created_at, updated_at) "
    "VALUES (:id, :org_id, :kind, :ref, :canonical_id, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP) "
    "ON CONFLICT (organisation_id, kind, ref) DO NOTHING"
)

# Advancing UPSERT. The evidence/run_count logic is per-row atomic:
#   * run_count always increments by :run_count_delta on the conflict arm;
#   * latest_* only overwrite when :evidence_ts > updated_at (compare-and-set);
#   * stage identity columns only change when a map-stage pipeline resolved
#     (:map_id IS NOT NULL); otherwise they are preserved unchanged.
# Bare column names in the DO UPDATE arm refer to the EXISTING row on both
# Postgres and SQLite (proposed values live under `excluded`).
_ADVANCE_SQL = text(
    "INSERT INTO journeys ("
    "id, organisation_id, kind, ref, canonical_work_item_id, "
    "latest_terminal_run_id, latest_status, latest_provenance, "
    'map_id, map_version, stage_id, stage_name, "position", '
    "run_count, created_at, updated_at"
    ") VALUES ("
    ":id, :org_id, :kind, :ref, :canonical_id, "
    ":run_id, :status, :provenance, "
    ":map_id, :map_version, :stage_id, :stage_name, :position, "
    ":run_count_delta, CURRENT_TIMESTAMP, :evidence_ts"
    ") ON CONFLICT (organisation_id, kind, ref) DO UPDATE SET "
    "latest_terminal_run_id = CASE "
    "  WHEN (:evidence_ts > updated_at OR updated_at IS NULL) AND :run_id IS NOT NULL THEN :run_id "
    "  ELSE latest_terminal_run_id END, "
    "latest_status = CASE "
    "  WHEN :evidence_ts > updated_at OR updated_at IS NULL THEN :status "
    "  ELSE latest_status END, "
    "latest_provenance = CASE "
    "  WHEN :evidence_ts > updated_at OR updated_at IS NULL THEN :provenance "
    "  ELSE latest_provenance END, "
    "map_id = CASE WHEN (:evidence_ts > updated_at OR updated_at IS NULL) AND :map_id IS NOT NULL "
    "  THEN :map_id ELSE map_id END, "
    "map_version = CASE WHEN (:evidence_ts > updated_at OR updated_at IS NULL) AND :map_id IS NOT NULL "
    "  THEN :map_version ELSE map_version END, "
    "stage_id = CASE WHEN (:evidence_ts > updated_at OR updated_at IS NULL) AND :map_id IS NOT NULL "
    "  THEN :stage_id ELSE stage_id END, "
    "stage_name = CASE WHEN (:evidence_ts > updated_at OR updated_at IS NULL) AND :map_id IS NOT NULL "
    "  THEN :stage_name ELSE stage_name END, "
    '"position" = CASE WHEN (:evidence_ts > updated_at OR updated_at IS NULL) AND :map_id IS NOT NULL '
    '  THEN :position ELSE "position" END, '
    "run_count = COALESCE(run_count, 0) + :run_count_delta, "
    "updated_at = CASE "
    "  WHEN :evidence_ts > updated_at OR updated_at IS NULL THEN :evidence_ts "
    "  ELSE updated_at END"
)


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


def _evidence_timestamp(completed_at: datetime | None, run_created_at: datetime) -> str:
    """Compare-and-set anchor: the run's ``completed_at``, falling back to its
    ``created_at`` when the run is not terminal (``awaiting_human``).

    Returned as a space-separated ISO string (``isoformat(sep=" ")``) rather
    than a ``datetime`` object: SQLAlchemy's SQLite DateTime processor stores
    exactly that format, so the ``text()`` comparison ``:evidence_ts >
    updated_at`` compares like-for-like on generic backends, while Postgres
    coerces the ISO string to timestamptz natively. Binding a raw ``datetime``
    into ``text()`` would route through the sqlite3 default datetime adapter,
    which is deprecated on Python 3.12 (and the test suite raises
    ``DeprecationWarning`` as an error).
    """
    anchor = completed_at if completed_at is not None else run_created_at
    return anchor.isoformat(sep=" ")


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
    ``source="reported"``). Returns ``(confirmed_entries, unmatched_count)``.
    The caller owns the RLS org context and an active transaction.
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
                )
            )
        ).scalar_one_or_none()
        if exists is not None:
            confirmed.append(entry)
        else:
            unmatched += 1
    return confirmed, unmatched


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

    stage: LifecycleMapStage | None = None
    if advancing and pipeline_id is not None:
        stage = await _resolve_stage_identity(session, organisation_id, pipeline_id)
    if advancing and stage is None and explicit_stage is not None:
        stage = explicit_stage

    advanced = 0
    seen: set[tuple[str, str]] = set()
    for entry in refs:
        canonical = _canonicalise_entry(entry)
        if canonical is None:
            continue
        key = (canonical["kind"], canonical["ref"])
        if key in seen:
            continue
        seen.add(key)

        params: dict[str, Any] = {
            "id": uuid.uuid4().hex,
            "org_id": organisation_id.hex,
            "kind": canonical["kind"],
            "ref": canonical["ref"],
            "canonical_id": canonical_work_item_id(organisation_id, canonical["kind"], canonical["ref"]).hex,
        }

        if not advancing:
            await session.execute(_MINT_SQL, params)
            continue

        await session.execute(
            _ADVANCE_SQL,
            {
                **params,
                "run_id": run_id.hex if run_id is not None else None,
                "status": status,
                "provenance": canonical.get("source", "derived"),
                "map_id": stage.map_id.hex if stage is not None else None,
                "map_version": stage.version if stage is not None else None,
                "stage_id": stage.stage_id if stage is not None else None,
                "stage_name": stage.stage_name if stage is not None else None,
                "position": stage.position if stage is not None else None,
                "run_count_delta": run_count_delta,
                "evidence_ts": evidence_ts,
            },
        )
        advanced += 1

    return advanced
