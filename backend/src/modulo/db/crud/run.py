"""CRUD for Run records.

All functions require RLS org context to be set by the caller.
"""

import asyncio
import hashlib
import json
import logging
import re
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from operator import attrgetter
from typing import Any, TypeGuard

from sqlalchemy import Date, bindparam, case, cast, delete, func, select, text, update
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer, selectinload

from modulo.core.exceptions import OrgDeletedError, RateLimitConflictError
from modulo.db.crud.base import PageResult
from modulo.db.crud.organisation import get_organisation
from modulo.db.crud.pagination import CursorPaginator
from modulo.db.crud.run_node_outputs import (
    DualWriteError,
    read_run_node_outputs_raw,
    replace_run_node_outputs,
)
from modulo.db.crud.team_scope import team_scope_clause
from modulo.db.lifecycle_refs import (
    _RESERVED_INPUT_PAYLOAD_KEYS,
    canonical_work_item_id,
    validate_ref_entry,
)
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.pipeline_snapshot import PipelineSnapshot
from modulo.db.models.run import (
    ACTIVE_RUN_STATUSES,
    AWAITING_HUMAN_STATUS,
    HITL_PARKED_STATUS,
    PIPELINE_CAPACITY_STATUSES,
    TERMINAL_STATUSES,
    Run,
)
from modulo.db.rls import OutputsRlsMismatch, read_rls_org
from modulo.db.sqlstates import DUAL_WRITE_RETRYABLE_SQLSTATES, sqlstate_of
from modulo.db.unique_violation import is_unique_violation

_log = logging.getLogger(__name__)

# Capacity-block reason markers (B5). Set on error_code when a run is demoted
# back to pending because a capacity limit was hit; distinct from terminal
# failure codes (never_dispatched, worker_lost, capacity_timeout, ...).
ERROR_CODE_ORG_CAPACITY_LIMITED = "org_capacity_limited"
ERROR_CODE_PIPELINE_CAPACITY = "pipeline_capacity"
ERROR_CODE_CAPACITY_TIMEOUT = "capacity_timeout"
# Non-terminal markers that operators must be able to distinguish from real
# failures. The stale-run sweep exempts runs carrying these markers.
CAPACITY_MARKERS = frozenset({ERROR_CODE_ORG_CAPACITY_LIMITED, ERROR_CODE_PIPELINE_CAPACITY})

# Day-key format used for run-usage bucketing and the --older-than parser.
_DAY_FORMAT = "%Y-%m-%d"

# FAR-438 run-record idempotency-key persistence. The run record stores its STABLE
# logical idempotency identity ``<pipeline_id>:<run_number>`` (NOT a per-replay
# ``run_id`` — a fresh UUID fork would mint a new key on every re-run and
# silently defeat dedupe). These helpers live HERE (the DB layer owns the run
# record) because import-linter forbids ``modulo.db`` importing ``modulo.core``;
# the derivation + dedupe that CONSUME the persisted value live in
# ``modulo.core.pipeline_engine.idempotency``. The ``<id>:<number>`` shape regex
# mirrors ``_RUN_REF_RE`` there; the two copies are a deliberate layering
# trade-off (DB cannot import core, so they cannot share one definition).
_RUN_IDEMPOTENCY_REF_RE = re.compile(r"^[A-Za-z0-9_-]+:\d+$")


def run_idempotency_ref(pipeline_id: uuid.UUID, run_number: int) -> str:
    """Build the stable logical run identity ``<pipeline_id>:<run_number>`` (FAR-438).

    This is the run-scoped value persisted on the run record. A re-run that
    restores the SAME run reuses the same ``run_number`` (allocated once per
    org), so the reference — and every per-node key derived from it — is stable
    across the re-run. It is deliberately NOT the per-replay ``run_id``.
    """
    return f"{pipeline_id}:{run_number}"


def run_idempotency_key(run_ref: str) -> str:
    """Validate *run_ref* and return the value to persist on the run record (FAR-438).

    Enforces the ``<id>:<number>`` contract at the persist boundary so a naive
    per-replay ``run_id`` fails loudly instead of silently persisting a value
    that mints a fresh key every re-run.
    """
    if not isinstance(run_ref, str) or not _RUN_IDEMPOTENCY_REF_RE.match(run_ref):
        raise ValueError(
            "run_ref must be the stable logical run identity '<pipeline_id>:<run_number>' "
            f"(recomputed on a re-run), NOT the per-replay run_id; got {run_ref!r}"
        )
    return run_ref


# Legacy underscore alias for the neutral helper (see modulo.db.unique_violation).
_is_unique_violation = is_unique_violation


# The canonical whitelist of run statuses (subset of the ``ck_runs_status``
# CHECK constraint). ``transition_run`` and ``update_run_status`` refuse any
# status outside this set (a typo would otherwise silently violate the CHECK
# constraint at commit time, or worse write an unknown status on backends
# without the constraint).
RUN_STATUS_WHITELIST: frozenset[str] = frozenset(
    {
        "pending",
        "running",
        "awaiting_human",
        "claimed",
        "unknown",
        "hitl_parked",
        "complete",
        "failed",
        "cancelled",
        "eval_failed",
        "stalled",
        "budget_exceeded",
        "router_no_match",
        "cost_ceiling_exceeded",
        "compensation_failed",
    }
)

# Trigger types exempt from the org-wide pause. A new trigger type defaults to
# PAUSED (fail-closed) unless explicitly added here AND it passes trigger_id to
# create_run (types that create runs without a Trigger row, like scheduled
# reports / variants, are NOT pause-gated — see the create_run gate comment).
PAUSE_EXEMPT_TRIGGER_TYPES = frozenset({"manual", "correction"})

# Stats-only failure groupings — NOT interchangeable with TERMINAL_STATUSES. The
# per-day "failed" bucket also counts the legacy ``expired`` spelling (a run
# demoted before the status-set cleanup), while the failure-reason breakdown
# only attributes genuinely failed executions (a cancelled run has no reason).
_FAILURE_BUCKET_STATUSES: frozenset[str] = frozenset(
    {"failed", "cancelled", "eval_failed", "expired", "stalled", "compensation_failed", "router_no_match"}
)
_FAILURE_REASON_STATUSES: frozenset[str] = frozenset(
    {"failed", "eval_failed", "stalled", "compensation_failed", "router_no_match"}
)

_SANDBOX_CONCURRENCY_KEY = "sandbox_concurrency_limit"
_SANDBOX_CONCURRENCY_MAX = 100
# FAR-589 D3b: the Docker-tier default for an ABSENT ``sandbox_concurrency_limit``
# key (ADR 029 as amended). The provider-filtered Docker-tier count that enforces
# this default is D8's; the reader only reports it (``is_default=True``). No
# numeric seeding anywhere — the default is resolved at read time, never written.
_SANDBOX_CONCURRENCY_DEFAULT = 4

_RUN_CONCURRENCY_KEY = "run_concurrency_limit"
_RUN_CONCURRENCY_MIN = 1
_RUN_CONCURRENCY_MAX = 100

# FAR-604 queue coalescing: the reserved payload field carrying the stable
# per-work-item coalesce key. System-injected (never forgeable via
# input_payload — the strip in create_run/coalesce_pending_run runs first),
# so the coalesce lookup can find a trigger's pending run for the same work
# item without a dedicated column. ``COALESCE_KEY_FIELD`` is the public alias
# consumed outside the db layer (hitl_manager gate coalescing reads the same
# stamp off runs reaching a HITL gate — FAR-604 D4).
_COALESCE_KEY_FIELD = "_coalesce_key"
COALESCE_KEY_FIELD = _COALESCE_KEY_FIELD
# Bounded candidate scan for non-Postgres backends (no server-side JSON path
# filter available): pending runs of ONE pipeline only. Public alias (qa F15):
# the HITL gate-coalescing scan imports the SAME cap so the two bounded-scan
# conventions cannot drift.
_COALESCE_CANDIDATE_LIMIT = 200
COALESCE_CANDIDATE_LIMIT = _COALESCE_CANDIDATE_LIMIT


def read_coalesce_key(payload: Any) -> str | None:
    """Read the coalesce key off a stored run input payload (qa F15).

    Shared extraction for every consumer that matches a run by its work-item
    key (``coalesce_pending_run``'s non-Postgres candidate scan and the HITL
    gate coalescing's entity match). Accepts the raw stored payload — a dict,
    a JSON string (some backends hand back TEXT), or None — and returns the
    key string or None when absent/falsy/foreign-shaped.
    """
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (ValueError, TypeError):
            payload = None
    if not isinstance(payload, dict):
        return None
    value = payload.get(_COALESCE_KEY_FIELD)
    return str(value) if value else None


def _is_terminal_status(status: str) -> bool:
    """Whether ``status`` ends the run (no further state transitions)."""
    return status in TERMINAL_STATUSES


def _is_failure_bucket_status(status: str) -> bool:
    """Whether ``status`` counts toward the per-day failed bucket in run stats."""
    return status in _FAILURE_BUCKET_STATUSES


def _is_failure_reason_status(status: str) -> bool:
    """Whether ``status`` can carry a failure reason in run stats."""
    return status in _FAILURE_REASON_STATUSES


def _is_valid_int_limit_value(value: Any) -> TypeGuard[int]:
    """Whether a settings_json value is a usable integer limit (never a bool)."""
    return not isinstance(value, bool) and isinstance(value, int)


def _input_hash(payload: dict[str, Any]) -> str:
    """Stable SHA-256 hex digest of a JSON-serialisable payload."""
    serialised = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(serialised.encode()).hexdigest()


def _strip_reserved_keys(input_payload: dict[str, Any]) -> dict[str, Any]:
    """Remove reserved system keys from ``input_payload`` before hash + storage.

    Reserved keys (``_work_item_id``, ``_modulo.work_item``,
    ``_feedback_correction``) are system-managed and must never be forgeable
    via webhook payloads or manual POST /runs bodies. Stripping centrally in
    ``create_run`` (the single chokepoint all paths funnel through) BEFORE the
    hash means an injected reserved key neither alters the run's hash nor
    reaches the stored payload. System data flows through explicit
    ``create_run`` kwargs, never ``input_payload``.
    """
    return {k: v for k, v in input_payload.items() if k not in _RESERVED_INPUT_PAYLOAD_KEYS}


async def _load_registered_guardrail_capabilities(
    session: AsyncSession,
    org_id: uuid.UUID,
    definitions: list[Any],
) -> dict[str, bool | None]:
    """Build the registered-capability map for conformance enforcement (item 7 "Plus").

    A block-action guardrail's ``required_capabilities`` is checked against
    the org's registered surfaces at dispatch time — here the active
    EnvironmentProfiles' ``capabilities_json``. Confirmed-present capabilities
    map True; everything else resolves to None (unreadable) so the fail-closed
    conformance derivation blocks (absent AND unknown both block). Only queried
    when at least one block-action guardrail carries a conformance claim, so
    the hot path stays free of the extra query.
    """
    needs_conformance = any(
        d.config.get("action") == "block" and (d.config.get("required_capabilities") or []) for d in definitions
    )
    if not needs_conformance:
        return {}
    from modulo.db.models.environment_profile import EnvironmentProfile

    profiles = (
        (
            await session.execute(
                select(EnvironmentProfile).where(
                    EnvironmentProfile.organisation_id == org_id,
                    EnvironmentProfile.deleted_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    registered: dict[str, bool | None] = {}
    for profile in profiles:
        for capability in profile.capabilities_json or []:
            registered[str(capability)] = True
    return registered


# Dedicated v5 namespace for floor work-item ids — DISTINCT from the journey
# canonical namespace so a floor chain anchor can never collide with a
# canonical journey id.
_FLOOR_NAMESPACE = uuid.UUID("4a1c3f6d-9e4b-4a1e-b5d2-3f7c8a9b0c1d")


def _floor_work_item_id(org_id: uuid.UUID, run_id: uuid.UUID) -> uuid.UUID:
    """Deterministic floor work-item id for a fresh run (no parent to adopt).

    Pure-Python, no DB round-trip — a stable function of (org, run) so the
    chain anchor is set exactly once at create and is reproducible.
    """
    return uuid.uuid5(_FLOOR_NAMESPACE, f"run:{org_id}:{run_id}")


async def _adopt_parent_work_item_id(
    session: AsyncSession,
    org_id: uuid.UUID,
    parent_run_id: uuid.UUID,
) -> uuid.UUID | None:
    """Adopt the parent run's ``work_item_id`` (agent_signal / correction children).

    Org-scoped RLS lookup wrapped in its own SAVEPOINT so a lookup failure
    rolls back only the read and cannot poison the caller's transaction.
    Returns ``None`` when the parent is missing or has no ``work_item_id``.
    """
    try:
        async with session.begin_nested():
            result = await session.execute(
                select(Run.work_item_id).where(Run.id == parent_run_id, Run.organisation_id == org_id)
            )
            return result.scalar_one_or_none()
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("work_item adoption lookup failed for parent run %s", parent_run_id)
        return None


async def _resolve_work_item_id(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    run_id: uuid.UUID,
    parent_run_id: uuid.UUID | None,
    explicit: uuid.UUID | None,
) -> uuid.UUID | None:
    """Resolve the run's ``work_item_id``: explicit > adopted-from-parent > floor.

    The chain anchor is written ONCE at create and never mutated afterwards.
    """
    if explicit is not None:
        return explicit
    if parent_run_id is not None:
        adopted = await _adopt_parent_work_item_id(session, org_id, parent_run_id)
        if adopted is not None:
            return adopted
    return _floor_work_item_id(org_id, run_id)


def _canonicalise_ref_entries(entries: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
    """Canonicalise + validate a list of raw work-item ref entries.

    Each entry goes through ``validate_ref_entry`` (canonical kind + ref, valid
    source/status). A malformed entry is dropped with a warning — the stamp is
    best-effort and must never abort run creation. Returns ``None`` for an
    empty/None input.
    """
    if not entries:
        return None
    canonical: list[dict[str, Any]] = []
    for entry in entries:
        try:
            canonical.append(validate_ref_entry(entry))
        except (ValueError, TypeError) as exc:
            _log.warning("dropping invalid work-item ref entry: %s", exc)
    return canonical or None


async def _hydrate_journeys(session: AsyncSession, org_id: uuid.UUID, refs: list[dict[str, Any]] | None) -> None:
    """Mint journey rows for the run's canonical work-item refs (create-time).

    ``INSERT ... ON CONFLICT (organisation_id, kind, ref) DO NOTHING`` — MINT
    ONLY: ``latest_*`` / ``run_count`` are owned by the finalise path (FAR-143)
    and are never touched here. Wrapped in its own SAVEPOINT and fail-open: a
    journey write failure logs + continues — a lost create-stamp is recoverable
    at finalise via the deterministic canonical id. A journey write failure
    must NEVER abort ``create_run``.
    """
    if not refs:
        return
    try:
        async with session.begin_nested():
            for entry in refs:
                canonical_id = canonical_work_item_id(org_id, entry["kind"], entry["ref"])
                # Hex-form UUID bindings for the raw INSERT — the portable form
                # that matches both Postgres (accepts 32-hex uuid input) and
                # SQLite (the Uuid type stores 32-char hex).
                await session.execute(
                    text(
                        "INSERT INTO journeys "
                        "(id, organisation_id, kind, ref, canonical_work_item_id, created_at, updated_at) "
                        "VALUES (:id, :org_id, :kind, :ref, :canonical_id, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP) "
                        "ON CONFLICT (organisation_id, kind, ref) DO NOTHING"
                    ),
                    {
                        "id": uuid.uuid4().hex,
                        "org_id": org_id.hex,
                        "kind": entry["kind"],
                        "ref": entry["ref"],
                        "canonical_id": canonical_id.hex,
                    },
                )
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("journey hydration failed for org %s", org_id)


_ATOMIC_RUN_NUMBER_SQL = text(
    "INSERT INTO run_number_counters (organisation_id, next_run_number) "
    "VALUES (:org_id, (SELECT COALESCE(MAX(run_number), 0) + 1 FROM runs WHERE organisation_id = :org_id)) "
    "ON CONFLICT (organisation_id) "
    "DO UPDATE SET next_run_number = CASE "
    "WHEN run_number_counters.next_run_number + 1 > "
    "(SELECT COALESCE(MAX(run_number), 0) + 1 FROM runs WHERE organisation_id = :org_id) "
    "THEN run_number_counters.next_run_number + 1 "
    "ELSE (SELECT COALESCE(MAX(run_number), 0) + 1 FROM runs WHERE organisation_id = :org_id) "
    "END "
    "RETURNING next_run_number"
)

_MAX_RUN_NUMBER_SQL = text("SELECT COALESCE(MAX(run_number), 0) + 1 FROM runs WHERE organisation_id = :org_id")


async def _allocate_run_number(session: AsyncSession, org_id: uuid.UUID) -> int:
    """Allocate the next ``run_number`` for *org_id* (FAR-168).

    Postgres uses a per-org atomic counter (``run_number_counters``) via
    ``INSERT ... ON CONFLICT DO UPDATE ... RETURNING``: concurrent creates in
    the same org serialize on the counter row and can never collide on
    ``uq_runs_org_run_number`` (the old ``MAX(run_number)+1`` raced under
    concurrent trigger dispatches — one transaction rolled back, the trigger
    missed a cycle and SAQ retried). Migration 0093 seeds the counter from the
    current ``MAX(run_number)`` per org so existing sequences continue without
    collision.

    The counter is self-healing on BOTH paths:

    * **No counter row yet** — the INSERT seed is
      ``COALESCE(MAX(run_number), 0) + 1`` from the ``runs`` table rather than a
      hardcoded ``1``, so orgs whose runs predate the counter (raw inserts that
      bypass ``create_run``, or an org created before migration 0093 without a
      seeded counter row) continue their existing sequence instead of colliding
      on ``run_number = 1``.
    * **Stale counter row** — a raw insert that bypasses the counter can leave
      the existing row behind ``MAX(run_number)``. The ``DO UPDATE`` takes the
      greater of ``counter + 1`` and ``MAX(run_number) + 1`` (via a portable
      ``CASE`` — ``GREATEST`` is not available on SQLite) so a counter that
      drifted below the actual max catches up instead of re-allocating an
      already-used number.

    Generic backends (SQLite/MariaDB) fall back to ``MAX(run_number)+1`` — a
    documented divergence: they are single-writer in practice and do not share
    the same upsert semantics. The visible contract is unchanged: ``run_number``
    is per-org, sequential, unique.
    """
    dialect = await _get_dialect_name(session)
    if dialect == "postgresql":
        result = await session.execute(
            _ATOMIC_RUN_NUMBER_SQL,
            # ``org_id.hex`` — portable UUID binding (see the org-guard comment).
            {"org_id": org_id.hex},
        )
        return int(result.scalar_one() or 1)
    result = await session.execute(_MAX_RUN_NUMBER_SQL, {"org_id": org_id.hex})
    return int(result.scalar_one() or 1)


async def _ensure_org_not_deleted(session: AsyncSession, org_id: uuid.UUID) -> None:
    """Soft-deleted-org guard (follow-up gap from the reconcile delivery).

    A run must never be created in an org whose deletion flow has set
    ``status='deleted'`` (or in a hard-deleted org — no row). Trigger-initiated
    runs already fail via ``ensure_triggers_resumable`` (a non-active status is
    treated as paused); this covers MANUAL runs (``trigger_id=None`` / exempt
    types) that bypass the pause gate. Read the status directly (never the ORM
    identity map) so a freshly toggled row is observed, mirroring
    ``org_is_paused``. Raised as ``OrgDeletedError`` (not ValueError) so
    routes/cron callers can map it to a structured 4xx instead of a generic
    500.
    """
    org_status_result = await session.execute(
        text("SELECT status FROM organisations WHERE id = :oid"),
        # ``org_id.hex`` (not ``str``) for raw text() UUID comparisons: SQLite's
        # Uuid type stores 32-char hex and never matches a dashed ``str(uuid)``;
        # Postgres accepts the bare 32-hex form as valid uuid input, so the
        # binding is portable across the supported backends.
        {"oid": org_id.hex},
    )
    org_status = org_status_result.scalar_one_or_none()
    if org_status is None or org_status == "deleted":
        raise OrgDeletedError(org_id=org_id, deleted=org_status == "deleted")


async def _read_guardrails_kill_switch(session: AsyncSession, org_id: uuid.UUID) -> bool:
    """Guardrails kill-switch (FAR-223 item 9) — pinned at run start.

    Pinned alongside the guardrail rows, never re-read mid-run. When ON, every
    bound guardrail downgrades to observe (shadow-only). A read failure (column
    absent on an unmigrated DB during bluegreen) defaults to OFF — normal
    enforcement stays active, which is the fail-closed direction for a
    data-safety control.
    """
    try:
        ks_row = (
            await session.execute(
                text("SELECT guardrails_kill_switch FROM organisations WHERE id = :oid"),
                {"oid": org_id.hex},
            )
        ).scalar_one_or_none()
        if ks_row is not None:
            return bool(ks_row)
    except SQLAlchemyError:
        _log.warning("guardrails.kill_switch_read_unavailable", extra={"org_id": str(org_id)})
    return False


async def _enforce_pause_gate(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    trigger_id: uuid.UUID | None,
    trigger_type: str,
) -> None:
    """Org-wide pause kill-switch — the SINGLE authority gate.

    Gate for trigger-initiated runs (webhook, replay, cron, polling,
    agent_signal). Manual runs (POST /runs, MCP trigger_pipeline), test_trigger
    (trigger_type="manual"), feedback correction, and variant runs pass
    (trigger_id None or an exempt type). A NEW trigger type defaults to PAUSED
    (fail-closed) unless explicitly added to PAUSE_EXEMPT_TRIGGER_TYPES AND it
    passes trigger_id.

    Accepted bounded TOCTOU: a run whose gate read ``not paused`` and whose
    INSERT commits after the toggle UPDATE lands is an "in-flight before pause"
    run — benign, matches GitHub disable-workflow semantics. Deliberately NO
    row locks (reviewed decision). Read failures PROPAGATE — a DB error is
    never fabricated into "paused". ``create_run`` calls
    ``ensure_triggers_resumable`` (modulo.db.settings_resolver), which raises
    ``TriggersPausedError`` (modulo.core.exceptions); that db->core edge is
    exempted under the ``db-does-not-import-core`` contract in ``.importlinter``.
    """
    if trigger_id is None or trigger_type in PAUSE_EXEMPT_TRIGGER_TYPES:
        return
    from modulo.db.settings_resolver import ensure_triggers_resumable

    await ensure_triggers_resumable(session, org_id, trigger_id=trigger_id, trigger_type=trigger_type)


@dataclass
class _GuardrailInterception:
    """Carried state from the ingestion-edge guardrail interception pass."""

    payload: dict[str, Any]
    results: list[Any]
    redactions: list[Any]
    blocked: bool
    block_message: str
    blocking_eval_name: str
    observed_by_eval: dict[uuid.UUID, bool]
    summary_json: dict[str, int] | None


@dataclass
class _InterceptionRequest:
    """Grouped inputs for the ingestion-edge guardrail interception pass."""

    org_id: uuid.UUID
    pipeline_id: uuid.UUID
    run_id: uuid.UUID
    payload: dict[str, Any]
    is_replay: bool | None
    snapshot_id: uuid.UUID
    guardrails_kill_switch: bool


def _has_guardrail_work(
    guardrail_rows: list[Any],
    pinned_defs: list[Any],
    skipped_guardrails: list[Any],
    guardrail_blocked: bool,
) -> bool:
    """Whether any bound guardrail row, pinned def, skip, or block needs evaluation."""
    return bool(guardrail_rows or pinned_defs or skipped_guardrails or guardrail_blocked)


def _has_pinned_guardrail_set(
    snap_pins: list[dict[str, Any]] | None,
    saved_fingerprint: str | None,
) -> bool:
    """A snapshot carries a pinned set when pins exist OR a fingerprint was saved.

    A saved fingerprint marks a PINNED snapshot even when its stored pin list is
    empty (a zeroed set is a tamper, not a legacy no-pin snapshot — FAR-309 PR B).
    """
    return bool(snap_pins or saved_fingerprint is not None)


def _pin_fingerprint_mismatch(saved_fingerprint: str | None, recomputed: str | None) -> bool:
    """A saved fingerprint that disagrees with the recomputed pins is a tamper."""
    return saved_fingerprint is not None and recomputed != saved_fingerprint


def _is_valid_pin_entry(entry: Any) -> bool:
    """A pin entry must be a dict carrying a non-empty ``name``."""
    return isinstance(entry, dict) and bool(entry.get("name"))


async def _load_snapshot_guardrail_pins(
    session: AsyncSession,
    org_id: uuid.UUID,
    snapshot_id: uuid.UUID,
) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Read a snapshot's pinned guardrail set + fingerprint (fail-open).

    Column/table absent on an unmigrated DB during bluegreen (or a backend that
    cannot resolve the column) → ``(None, None)`` so the replay falls back to
    the live rows (pre-pinning behaviour).
    """
    try:
        snap_pin_row = (
            await session.execute(
                select(
                    PipelineSnapshot.guardrail_pins_json,
                    PipelineSnapshot.guardrail_pins_fingerprint,
                ).where(PipelineSnapshot.id == snapshot_id)
            )
        ).one_or_none()
        if snap_pin_row is not None:
            return snap_pin_row[0], snap_pin_row[1]
    except SQLAlchemyError:
        _log.warning("guardrails.pins_read_unavailable", extra={"org_id": str(org_id)})
    return None, None


async def _rebuild_pinned_guardrail_defs(
    *,
    org_id: uuid.UUID,
    run_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    guardrail_rows: list[Any],
    snap_pins: list[dict[str, Any]] | None,
    saved_fingerprint: str | None,
) -> tuple[list[Any], list[Any], bool, str]:
    """Re-verify the pinned fingerprint and rebuild engine defs from the pins.

    A fingerprint mismatch fails CLOSED (blocked=True); soft-deleted or
    unreadable pins become GuardrailSkips (never a run failure). Returns
    ``(pinned_defs, skipped_guardrails, blocked, block_message)``.
    """
    from modulo.core.guardrails import (
        GuardrailSkip,
        fingerprint_guardrail_pins,
        notify_guardrail_event,
        to_engine_definition_from_pin,
    )

    pinned_defs: list[Any] = []
    skipped_guardrails: list[GuardrailSkip] = []
    if not _has_pinned_guardrail_set(snap_pins, saved_fingerprint):
        return pinned_defs, skipped_guardrails, False, ""

    recomputed_fingerprint = fingerprint_guardrail_pins(snap_pins)
    if _pin_fingerprint_mismatch(saved_fingerprint, recomputed_fingerprint):
        block_message = "guardrail mechanism error: snapshot guardrail pin fingerprint mismatch"
        _log.error(
            "guardrails.pin_fingerprint_mismatch",
            extra={"org_id": str(org_id), "snapshot_id": str(snapshot_id)},
        )
        await notify_guardrail_event(
            org_id,
            "guardrail_enforcement_gap",
            {
                "guardrail": "<snapshot-pins>",
                "reason": "pin_fingerprint_mismatch",
                "detail": "snapshot guardrail pin fingerprint mismatch at run start",
                "snapshot_id": str(snapshot_id),
                "run_id": str(run_id),
            },
            run_id=run_id,
        )
        return pinned_defs, skipped_guardrails, True, block_message

    if not snap_pins:
        return pinned_defs, skipped_guardrails, False, ""

    live_by_name = {row.name: row for row in guardrail_rows}
    for entry in snap_pins:
        if not _is_valid_pin_entry(entry):
            continue
        name = str(entry["name"])
        if name not in live_by_name:
            skipped_guardrails.append(GuardrailSkip(name=name, reason="soft_deleted"))
            continue
        try:
            pinned_defs.append(to_engine_definition_from_pin(entry))
        except Exception:
            _log.exception("guardrails.pin_rebuild_error", extra={"guardrail": name})
            skipped_guardrails.append(GuardrailSkip(name=name, reason="soft_deleted", detail="pin unreadable"))
    return pinned_defs, skipped_guardrails, False, ""


def _select_guardrail_definitions(
    guardrail_rows: list[Any],
    pinned_defs: list[Any],
    snap_pins: list[dict[str, Any]] | None,
    saved_fingerprint: str | None,
) -> list[Any]:
    """Item 10 invariant: pinned defs when the snapshot is pinned, else live rows.

    A snapshot with a NON-EMPTY pinned set evaluates exactly that set, even when
    EVERY pin fails to rebuild. Only a snapshot with NO pins (pre-migration /
    read failure) falls back to the live rows.
    """
    if _has_pinned_guardrail_set(snap_pins, saved_fingerprint):
        return pinned_defs
    from modulo.core.guardrails import to_engine_definition

    return [to_engine_definition(row) for row in guardrail_rows]


def _downgrade_guardrails_to_observe(guardrail_defs: list[Any]) -> list[Any]:
    """Item 9 — kill-switch: downgrade EVERY bound guardrail to observe."""
    from modulo.core.guardrails import GuardrailAction

    downgraded = []
    for d in guardrail_defs:
        cfg = dict(d.config)
        cfg["action"] = GuardrailAction.OBSERVE.value
        downgraded.append(d.model_copy(update={"config": cfg}))
    return downgraded


async def _run_guardrail_interception_pass(
    *,
    org_id: uuid.UUID,
    _run_id: uuid.UUID,
    guardrail_defs: list[Any],
    payload: dict[str, Any],
    is_replay: bool | None,
    skipped_guardrails: list[Any],
    any_guarding: bool,
) -> tuple[dict[str, Any], list[Any], list[Any], bool, str, list[Any], str]:
    """Execute the interception pass and return every mutated run state field.

    Fail-closed for block/redact guardrails (any_guarding); observe/warn-only
    guardrails log-and-continue. Emits the interception latency metric. Returns
    ``(payload, results, redactions, blocked, block_message, skipped,
    blocking_eval_name)`` in assignment order.
    """
    from modulo.core.eval_engine import EvalEngine
    from modulo.core.guardrails import run_interception_pass_async

    start_wall = time.perf_counter()
    try:
        outcome = await run_interception_pass_async(
            EvalEngine(),
            guardrail_defs,
            payload,
            detection_only=bool(is_replay),
            skipped=skipped_guardrails,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("guardrails.interception_error")
        if any_guarding:
            payload_out: dict[str, Any] = payload
            results: list[Any] = []
            redactions: list[Any] = []
            blocked = True
            block_message = "guardrail mechanism error at ingestion edge"
        else:
            payload_out = payload
            results = []
            redactions = []
            blocked = False
            block_message = ""
        latency_ms = (time.perf_counter() - start_wall) * 1000
        _log.info(
            "guardrails.interception_latency_ms",
            extra={
                "org_id": str(org_id),
                "guardrail_count": len(guardrail_defs),
                "latency_ms": round(latency_ms, 3),
            },
        )
        return payload_out, results, redactions, blocked, block_message, skipped_guardrails, ""
    latency_ms = (time.perf_counter() - start_wall) * 1000
    _log.info(
        "guardrails.interception_latency_ms",
        extra={
            "org_id": str(org_id),
            "guardrail_count": len(guardrail_defs),
            "latency_ms": round(latency_ms, 3),
        },
    )
    return (
        outcome.payload,
        outcome.results,
        outcome.redactions,
        outcome.blocked,
        outcome.block_message,
        outcome.skipped,
        outcome.blocking_eval_name,
    )


async def _enforce_guardrail_conformance(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    run_id: uuid.UUID,
    guardrail_defs: list[Any],
) -> tuple[bool, str]:
    """Conformance enforcement (FAR-223 item 7 "Plus").

    A block-action guardrail carrying ``required_capabilities`` that the org
    cannot satisfy blocks (fail closed — absent AND unknown) and fires a paging
    Notification via the alert path. Returns ``(blocked, block_message)``.
    """
    from modulo.core.guardrails import non_conformant_blocking_guardrails, notify_guardrail_event

    conformance_registered = await _load_registered_guardrail_capabilities(session, org_id, guardrail_defs)
    for eval_def, derivation in non_conformant_blocking_guardrails(guardrail_defs, conformance_registered):
        _log.warning(
            "guardrails.conformance_block",
            extra={
                "org_id": str(org_id),
                "guardrail": eval_def.name,
                "state": derivation.state,
            },
        )
        block_message = (
            f"guardrail {eval_def.name!r} non-conformant: required capabilities unavailable "
            f"({', '.join(derivation.missing + derivation.unreadable) or 'unknown'})"
        )
        await notify_guardrail_event(
            org_id,
            "guardrail_enforcement_gap",
            {
                "guardrail": eval_def.name,
                "reason": "non_conformant",
                "state": derivation.state,
                "run_id": str(run_id),
            },
            run_id=run_id,
        )
        return True, block_message
    return False, ""


async def _audit_and_alert_skipped_guardrails(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    run_id: uuid.UUID,
    skipped_guardrails: list[Any],
) -> None:
    """Item 10/11 — audit + alert skipped pinned guardrails (best-effort)."""
    from modulo.core.guardrails import (
        GUARDRAIL_SKIP_EXPECTED_REASONS,
        alert_unexpected_guardrail_skip,
        audit_guardrail_skip,
    )

    for skip in skipped_guardrails:
        await audit_guardrail_skip(session, org_id, run_id, skip)
        if skip.reason not in GUARDRAIL_SKIP_EXPECTED_REASONS:
            await alert_unexpected_guardrail_skip(org_id, run_id, skip)


async def _derive_guardrail_summary(
    *,
    org_id: uuid.UUID,
    run_id: uuid.UUID,
    guardrail_defs: list[Any],
    guardrail_results: list[Any],
    guardrail_redactions: list[Any],
    skipped_guardrails: list[Any],
    guardrail_observed_by_eval: dict[uuid.UUID, bool],
) -> dict[str, int] | None:
    """Item 11 — guardrail_summary telemetry snapshot + fired-signature log.

    TELEMETRY: best-effort fail-open — a summary-derivation failure must never
    break run creation; it degrades to no summary + a log.
    """
    from modulo.core.guardrails import (
        build_guardrail_summary,
        log_guardrail_fired_signatures,
    )

    try:
        summary_json = build_guardrail_summary(
            bound=len(guardrail_defs) + len(skipped_guardrails),
            definitions=guardrail_defs,
            results=guardrail_results,
            redactions=guardrail_redactions,
            skipped=skipped_guardrails,
            observed_by_eval=guardrail_observed_by_eval,
        ).to_dict()
        log_guardrail_fired_signatures(
            org_id=org_id,
            run_id=run_id,
            definitions=guardrail_defs,
            results=guardrail_results,
        )
        return summary_json
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("guardrails.summary_derive_failed", extra={"run_id": str(run_id)})
        return None


@dataclass
class _PinnedGuardrailState:
    """Replay pin-resolution output: pinned defs + skips + any pin-fingerprint block."""

    pinned_defs: list[Any]
    skipped_guardrails: list[Any]
    blocked: bool
    block_message: str
    snap_pins: list[dict[str, Any]] | None
    saved_fingerprint: str | None


async def _resolve_pinned_guardrail_state(
    session: AsyncSession,
    request: _InterceptionRequest,
    guardrail_rows: list[Any],
) -> _PinnedGuardrailState:
    """Item 10 — resolve the replay's PINNED guardrail set (or the live-row fallback).

    A replay uses the pinned guardrail set from the snapshot, not the live
    rows. A pinned guardrail whose live row no longer exists (soft-deleted) is
    SKIPPED (never a run failure) with an audit event + enforcement-gap alert.
    A snapshot with no pins (pre-migration) falls back to the live rows.
    Non-replay runs carry an empty pinned state.
    """
    if not (request.is_replay and request.snapshot_id is not None):
        return _PinnedGuardrailState(
            pinned_defs=[],
            skipped_guardrails=[],
            blocked=False,
            block_message="",
            snap_pins=None,
            saved_fingerprint=None,
        )
    snap_pins, saved_fingerprint = await _load_snapshot_guardrail_pins(session, request.org_id, request.snapshot_id)
    pinned_defs, skipped_guardrails, blocked, block_message = await _rebuild_pinned_guardrail_defs(
        org_id=request.org_id,
        run_id=request.run_id,
        snapshot_id=request.snapshot_id,
        guardrail_rows=guardrail_rows,
        snap_pins=snap_pins,
        saved_fingerprint=saved_fingerprint,
    )
    return _PinnedGuardrailState(
        pinned_defs=pinned_defs,
        skipped_guardrails=skipped_guardrails,
        blocked=blocked,
        block_message=block_message,
        snap_pins=snap_pins,
        saved_fingerprint=saved_fingerprint,
    )


async def _run_guardrail_gate(
    session: AsyncSession,
    request: _InterceptionRequest,
    *,
    guardrail_rows: list[Any],
    pinned: _PinnedGuardrailState,
) -> _GuardrailInterception:
    """Run the enforcement gate over the selected guardrail definitions.

    Stage order (FAR-208): definition selection → cap enforcement (fail
    closed) → kill-switch downgrade → conformance enforcement → the
    interception pass (only when not already blocked — a pin-fingerprint or
    conformance block must never be cleared) → skip audit/alert → summary
    telemetry. See :func:`_intercept_guardrails` for the documented invariants
    each stage preserves.
    """
    from modulo.core.guardrails import GuardrailAction, guardrail_cap_violation

    org_id = request.org_id
    run_id = request.run_id
    payload = request.payload
    skipped_guardrails = pinned.skipped_guardrails

    guardrail_defs = _select_guardrail_definitions(
        guardrail_rows, pinned.pinned_defs, pinned.snap_pins, pinned.saved_fingerprint
    )

    observed_by_eval: dict[uuid.UUID, bool] = {}
    results: list[Any] = []
    redactions: list[Any] = []
    blocking_eval_name = ""

    # Item 7 — cap enforcement (fail closed): a single node binding more
    # than the per-node guardrail cap is a mechanism error. Graph-save
    # rejects the authoring-time case; this is the defensive backstop.
    cap_violation = guardrail_cap_violation(guardrail_defs)
    if cap_violation:
        _log.warning("guardrails.cap_violation", extra={"org_id": str(org_id), "detail": cap_violation})
        blocked = True
        block_message = f"guardrail mechanism error: {cap_violation}"
    else:
        # Item 9 — kill-switch: downgrade EVERY bound guardrail to observe
        # (shadow-only — compute + log, never block, never redact). Never a
        # full disable: observe mode still computes and logs.
        if request.guardrails_kill_switch:
            guardrail_defs = _downgrade_guardrails_to_observe(guardrail_defs)
            _log.warning("guardrails.kill_switch_active", extra={"org_id": str(org_id)})

        observed_by_eval = {d.id: d.config.get("action") == GuardrailAction.OBSERVE for d in guardrail_defs}
        any_guarding = any(
            d.config.get("action") in (GuardrailAction.BLOCK, GuardrailAction.REDACT) for d in guardrail_defs
        )

        # Conformance enforcement (FAR-223 item 7 "Plus"): a block-action
        # guardrail carrying required_capabilities that the org cannot
        # satisfy blocks (fail closed — absent AND unknown) and fires a
        # paging Notification via the alert path. The derivation helper is
        # shipped; this is its dispatch-time wiring. Only applied when a
        # conformance block actually fires — a clean conformance result must
        # never clear a block already set by the pin-fingerprint check.
        conformance_blocked, conformance_message = await _enforce_guardrail_conformance(
            session,
            org_id=org_id,
            run_id=run_id,
            guardrail_defs=guardrail_defs,
        )
        blocked = pinned.blocked or conformance_blocked
        block_message = conformance_message if conformance_blocked else pinned.block_message

        if not blocked:
            (
                payload,
                results,
                redactions,
                blocked,
                block_message,
                skipped_guardrails,
                blocking_eval_name,
            ) = await _run_guardrail_interception_pass(
                org_id=org_id,
                _run_id=run_id,
                guardrail_defs=guardrail_defs,
                payload=payload,
                is_replay=request.is_replay,
                skipped_guardrails=skipped_guardrails,
                any_guarding=any_guarding,
            )
        # NOTE (item 10 invariant): a conformance block (``blocked`` True via
        # the block above) must NOT clear the accumulated pin-skips collected
        # earlier in this seam — they survive the conformance path and are
        # still audited + alerted just below. The pass only ever replaces
        # ``skipped_guardrails`` with its own carried skips (via
        # ``outcome.skipped``), so there is no stale-skip case to clear.

    # Item 10 — audit + alert skipped pinned guardrails (best-effort: the
    # skip is the policy; a failed audit/alert never breaks the run).
    # Item 11 — a skip NOT explained by soft-deleted pin state is
    # UNEXPECTED and pages an additional ``guardrail_unexpected_skip``
    # alert (Notification Log + Error Forwarders).
    await _audit_and_alert_skipped_guardrails(
        session,
        org_id=org_id,
        run_id=run_id,
        skipped_guardrails=skipped_guardrails,
    )

    # Item 11 — guardrail_summary telemetry snapshot + per-pattern
    # fired-signature regression log. Computed BEFORE the run row exists so
    # it can be persisted on the Run in one place. ``bound`` = the guardrail
    # rows bound at run start (pinned set or live fallback) INCLUDING
    # skipped pins, so ``evaluated + errored + skipped == bound`` holds by
    # construction (build_guardrail_summary absorbs no-clean-detection
    # guardrails into ``errored``). TELEMETRY: best-effort fail-open — a
    # summary-derivation failure must never break run creation (the
    # enforcement already happened); it degrades to no summary + a log.
    summary_json = await _derive_guardrail_summary(
        org_id=org_id,
        run_id=run_id,
        guardrail_defs=guardrail_defs,
        guardrail_results=results,
        guardrail_redactions=redactions,
        skipped_guardrails=skipped_guardrails,
        guardrail_observed_by_eval=observed_by_eval,
    )

    return _GuardrailInterception(
        payload=payload,
        results=results,
        redactions=redactions,
        blocked=blocked,
        block_message=block_message,
        blocking_eval_name=blocking_eval_name,
        observed_by_eval=observed_by_eval,
        summary_json=summary_json,
    )


async def _intercept_guardrails(
    session: AsyncSession,
    request: _InterceptionRequest,
) -> _GuardrailInterception:
    """Guardrail interception (FAR-208 item 2) — the ingestion edge.

    The two-phase pass runs BEFORE the run's input_payload is persisted, so
    persisted state is post-redaction. A block outcome creates the run as a
    TERMINAL eval_failed run instead of a pending/executable one. Replays
    (``is_replay=True``) are detection-only — no act, no re-block (item 10).

    Mechanism errors FAIL CLOSED when any bound guardrail carries a block or
    redact action (item 7: warn-on-error applies to warn-action only);
    observe/warn-only guardrails log-and-continue on mechanism error.
    """
    from modulo.db.crud.guardrail_config import load_pipeline_guardrail_rows

    guardrail_rows = await load_pipeline_guardrail_rows(
        session,
        pipeline_id=request.pipeline_id,
        organisation_id=request.org_id,
    )
    pinned = await _resolve_pinned_guardrail_state(session, request, guardrail_rows)

    if _has_guardrail_work(guardrail_rows, pinned.pinned_defs, pinned.skipped_guardrails, pinned.blocked):
        return await _run_guardrail_gate(session, request, guardrail_rows=guardrail_rows, pinned=pinned)
    return _GuardrailInterception(
        payload=request.payload,
        results=[],
        redactions=[],
        blocked=pinned.blocked,
        block_message=pinned.block_message,
        blocking_eval_name="",
        observed_by_eval={},
        summary_json=None,
    )


def _inject_engine_payload_keys(
    stored_payload: dict[str, Any],
    feedback_correction: dict[str, Any] | None,
    coalesce_key: str | None,
) -> dict[str, Any]:
    """Stamp engine-only payload keys AFTER the reserved-key strip (never forgeable).

    ``_feedback_correction`` (FAR-142): the key is reserved and stripped, so a
    user payload can never forge correction-run context. Correction runs flow
    the value through the explicit ``feedback_correction`` kwarg instead — the
    value still reaches the stored input_payload (and executor._seed_state's
    promotion to run_context), but only engine callers can set it.

    ``coalesce_key`` (FAR-604): the stable coalesce key is stamped AFTER the
    strip (system-managed, never forgeable) so the pending run is findable by
    coalesce_pending_run. Included in input_hash like the rest of the stored
    payload.
    """
    if feedback_correction is not None:
        stored_payload["_feedback_correction"] = feedback_correction
    if coalesce_key is not None:
        stored_payload[_COALESCE_KEY_FIELD] = coalesce_key
    return stored_payload


def _stamp_guardrail_blocked_run(run: Run, guardrail_block_message: str) -> None:
    """Stamp a guardrail-blocked run TERMINAL (eval_failed) at the ingestion edge.

    The run is created only so the failure is visible in the run list. It is
    NEVER dispatched to the executor (dispatch_run refuses terminal runs),
    never retried, and has no HITL gate to resume.
    """
    run.status = "eval_failed"
    run.error_code = "eval_blocked"
    run.error_detail = guardrail_block_message
    run.completed_at = datetime.now(UTC)


async def _resolve_owner_team_id(
    session: AsyncSession,
    owner_team_id: uuid.UUID | None,
    pipeline_id: uuid.UUID,
) -> uuid.UUID | None:
    """Team-boundary stamping: inherit the pipeline's owner team when none passed.

    ``Run.owner_team_id`` is the source of truth for the MCP team-boundary
    guards and the analytics facts (``RunDailyFact.team_id``); without this
    stamp, every guard reads a NULL owner and silently treats cross-team runs
    as org-level.
    """
    if owner_team_id is not None:
        return owner_team_id
    return (
        await session.execute(select(Pipeline.owner_team_id).where(Pipeline.id == pipeline_id))
    ).scalar_one_or_none()


async def _persist_guardrail_eval_results(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    run_id: uuid.UUID,
    guardrail_results: list[Any],
    guardrail_observed_by_eval: dict[uuid.UUID, bool],
) -> None:
    """Persist guardrail eval results (evidence deltas vs the pre-act base).

    detail is count-only / pattern-descriptive — never raw payload (item 7
    no-raw-persist). Observe-mode guardrails stamp observed=True so the
    guardrail_summary observed bucket is counted exactly once.
    """
    from modulo.db.models.eval_definition import EvalDefinition as _EvalDefinitionModel
    from modulo.db.models.eval_result import EvalResult as EvalResultModel

    # FAR-382: stamp the definition version snapshot so a later rubric bump
    # never makes this guardrail outcome look like a regression. The engine
    # DTO carries no version, so resolve it from the (org-scoped) definition
    # rows in one batched query rather than N+1.
    guardrail_eval_ids = {gr.eval_id for gr in guardrail_results}
    version_by_eval_id: dict[uuid.UUID, int] = {}
    if guardrail_eval_ids:
        def_rows = (
            await session.execute(
                select(_EvalDefinitionModel.id, _EvalDefinitionModel.version).where(
                    _EvalDefinitionModel.id.in_(guardrail_eval_ids),
                    _EvalDefinitionModel.organisation_id == org_id,
                )
            )
        ).all()
        version_by_eval_id = {row.id: row.version for row in def_rows}

    for gr in guardrail_results:
        session.add(
            EvalResultModel(
                organisation_id=org_id,
                run_id=run_id,
                node_id=None,
                eval_id=gr.eval_id,
                eval_definition_version=version_by_eval_id.get(gr.eval_id),
                passed=gr.passed,
                score=gr.score,
                detail=(gr.detail or "")[:2000],
                observed=guardrail_observed_by_eval.get(gr.eval_id, False),
            )
        )


async def _compensate_blocked_run_best_effort(
    session: AsyncSession,
    run: Run,
    *,
    run_id: uuid.UUID,
    guardrail_block_message: str,
    guardrail_blocking_eval_name: str,
) -> None:
    """Run-termination compensation (FAR-213) — best-effort + failure-isolated.

    Runs AFTER the terminal status write (the run was flushed above): writes
    the blocked_partial summary and, when a connector hub is supplied,
    compensates executed nodes' external side effects. It must NEVER block or
    delay the terminal write and never propagate — guard-the-guard: any
    compensation raise is logged + audited here. At the ingestion edge no
    nodes have executed (connector_hub is always None here), so only the
    summary + summary audit are written; the mid-run terminalization paths
    call compensate_blocked_run directly with the executed node outputs and a
    connector hub.
    """
    from modulo.core.guardrails.compensation import compensate_blocked_run

    try:
        await compensate_blocked_run(
            session,
            run,
            guardrail_block=guardrail_block_message,
            blocking_eval_name=guardrail_blocking_eval_name,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("guardrails.compensation.error run=%s", run_id)


async def create_run(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    trigger_type: str,
    input_payload: dict[str, Any],
    account_id: uuid.UUID | None = None,
    trigger_id: uuid.UUID | None = None,
    owner_team_id: uuid.UUID | None = None,
    parent_run_id: uuid.UUID | None = None,
    rate_limit_key: str | None = None,
    work_item_id: uuid.UUID | None = None,
    work_item_refs: list[dict[str, Any]] | None = None,
    is_replay: bool | None = None,
    variant_group_id: uuid.UUID | None = None,
    batch_id: uuid.UUID | None = None,
    variant_config_snapshot: dict[str, Any] | None = None,
    feedback_correction: dict[str, Any] | None = None,
    coalesce_key: str | None = None,
) -> Run:
    # Soft-deleted-org guard — a run must never be created in an org whose
    # deletion flow has set status='deleted' (or in a hard-deleted org).
    await _ensure_org_not_deleted(session, org_id)

    # DB capacity hard-stop (FAR-426): refuse a NEW run when a *fixed* DB is
    # at/over the 98% threshold. This is the boundary where a run is created —
    # a resume, retention sweep or admin operation never reaches it. Fail-open:
    # a broken capacity probe can never block run creation (it allows the run).
    # Raises ``StorageExhaustedError`` (modulo.db.capacity); the API layer maps
    # it to HTTP 503 ``urn:problem:modulo:storage_exhausted`` so the transaction
    # here rolls back and no phantom run is persisted.
    from modulo.db.capacity import enforce_capacity_gate

    await enforce_capacity_gate()

    # Guardrails kill-switch (FAR-223 item 9) — pinned at run start alongside
    # the guardrail rows, never re-read mid-run. Defaults to OFF on read
    # failure (the fail-closed direction for a data-safety control).
    guardrails_kill_switch = await _read_guardrails_kill_switch(session, org_id)

    # Org-wide pause kill-switch — the SINGLE authority gate for
    # trigger-initiated runs (webhook, replay, cron, polling, agent_signal).
    # Manual runs (POST /runs, MCP trigger_pipeline), test_trigger
    # (trigger_type="manual"), feedback correction, and variant runs pass
    # (trigger_id None or an exempt type).
    await _enforce_pause_gate(
        session,
        org_id,
        trigger_id=trigger_id,
        trigger_type=trigger_type,
    )

    # Reserved-key strip (FAR-142 security control, ALWAYS-ON): reserved keys
    # are system-managed and must never be forgeable via input_payload. The
    # strip happens BEFORE _input_hash() so an injected reserved key cannot
    # alter the run's hash, and the STRIPPED payload is what gets stored.
    stored_payload = _strip_reserved_keys(input_payload)

    # Guardrail interception (FAR-208 item 2) — the ingestion edge. The
    # two-phase pass runs BEFORE the run's input_payload is persisted, so
    # persisted state is post-redaction. A block outcome creates the run as a
    # TERMINAL eval_failed run instead of a pending/executable one. Replays
    # (is_replay=True) are detection-only — no act, no re-block (item 10).
    run_id = uuid.uuid4()
    interception = await _intercept_guardrails(
        session,
        _InterceptionRequest(
            org_id=org_id,
            pipeline_id=pipeline_id,
            run_id=run_id,
            payload=stored_payload,
            is_replay=is_replay,
            snapshot_id=snapshot_id,
            guardrails_kill_switch=guardrails_kill_switch,
        ),
    )
    stored_payload = interception.payload
    guardrail_results = interception.results
    guardrail_blocked = interception.blocked
    guardrail_block_message = interception.block_message
    guardrail_blocking_eval_name = interception.blocking_eval_name
    guardrail_observed_by_eval = interception.observed_by_eval
    guardrail_summary_dict = interception.summary_json

    stored_payload = _inject_engine_payload_keys(stored_payload, feedback_correction, coalesce_key)

    thread_id = f"{org_id}:{run_id}"
    # Per-org atomic counter (FAR-168) — never MAX(run_number)+1 on Postgres,
    # which races under concurrent trigger dispatches.
    run_number = await _allocate_run_number(session, org_id)

    # FAR-438 idempotency-key persistence: the run's STABLE logical identity
    # (<pipeline_id>:<run_number>) is written once at create so a re-run that
    # restores THIS run can READ it back and reuse the identical derived per-node
    # keys (UNKNOWN-recovery dedupe). It is NEVER a per-replay run_id (a fresh
    # UUID fork would mint a new key every re-run). run_idempotency_key validates
    # the <id>:<number> shape so a buggy identity fails loudly at the persist
    # boundary rather than silently storing a value that mints fresh keys. These
    # helpers are local (DB layer) — the derivation that CONSUMES the stored value
    # lives in modulo.core.pipeline_engine.idempotency (a module the DB layer
    # must not import, per import-linter).
    idempotency_key = run_idempotency_key(run_idempotency_ref(pipeline_id, run_number))

    # Create-time journey stamping (FAR-142): resolve the chain anchor
    # (explicit > adopted-from-parent > deterministic floor), canonicalise the
    # work-item refs, and carry is_replay / variant_group_id verbatim. None of
    # this is read back out of input_payload — system data flows via kwargs.
    resolved_work_item_id = await _resolve_work_item_id(
        session,
        org_id=org_id,
        run_id=run_id,
        parent_run_id=parent_run_id,
        explicit=work_item_id,
    )
    canonical_refs = _canonicalise_ref_entries(work_item_refs)

    owner_team_id = await _resolve_owner_team_id(session, owner_team_id, pipeline_id)

    run = Run(
        id=run_id,
        organisation_id=org_id,
        pipeline_id=pipeline_id,
        snapshot_id=snapshot_id,
        trigger_type=trigger_type,
        input_hash=_input_hash(stored_payload),
        input_payload=stored_payload,
        account_id=account_id,
        trigger_id=trigger_id,
        owner_team_id=owner_team_id,
        langgraph_thread_id=thread_id,
        parent_run_id=parent_run_id,
        run_number=run_number,
        rate_limit_key=rate_limit_key,
        idempotency_key=idempotency_key,
        work_item_id=resolved_work_item_id,
        work_item_refs=canonical_refs,
        is_replay=is_replay,
        variant_group_id=variant_group_id,
        batch_id=batch_id,
        variant_config_snapshot=variant_config_snapshot,
        guardrail_summary_json=guardrail_summary_dict,
    )
    if guardrail_blocked:
        _stamp_guardrail_blocked_run(run, guardrail_block_message)
    try:
        # Commit the insert inside a savepoint so that, on the async/Postgres
        # backend, a concurrent rate-limit conflict aborts only the nested
        # transaction (not the outer one). Without this the failed flush leaves
        # the outer transaction in a failed/aborted state, and the caller's
        # ``except RateLimitConflictError`` handler can no longer write its
        # rate-limit TriggerEvent (its own flush raises PendingRollbackError).
        #
        # ``session.add(run)`` must live INSIDE the savepoint: a savepoint
        # rollback restores the session's pending-insert queue, so on conflict
        # the ``run`` row is dropped from the queue and the outer transaction
        # stays clean. Adding it OUTSIDE would leave the failed run pending,
        # so the caller's next flush re-inserts it, conflicts *outside* any
        # savepoint, and aborts the whole transaction (PendingRollbackError).
        async with session.begin_nested():
            session.add(run)
            await session.flush()
    except IntegrityError as exc:
        if rate_limit_key is not None and _is_unique_violation(exc):
            raise RateLimitConflictError(
                pipeline_id=pipeline_id,
                rate_limit_key=rate_limit_key,
            ) from exc
        raise

    if guardrail_results:
        await _persist_guardrail_eval_results(
            session,
            org_id=org_id,
            run_id=run_id,
            guardrail_results=guardrail_results,
            guardrail_observed_by_eval=guardrail_observed_by_eval,
        )

    # Journey hydration (mint-only, fail-open). A journey write failure must
    # NEVER abort create_run — a lost create-stamp is recoverable at finalise
    # via the deterministic canonical id.
    await _hydrate_journeys(session, org_id, canonical_refs)

    if guardrail_blocked:
        await _compensate_blocked_run_best_effort(
            session,
            run,
            run_id=run_id,
            guardrail_block_message=guardrail_block_message,
            guardrail_blocking_eval_name=guardrail_blocking_eval_name,
        )
    return run


async def coalesce_pending_run(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    coalesce_key: str,
    input_payload: dict[str, Any],
) -> Run | None:
    """Fold a new trigger delivery into the pipeline's UNSTARTED pending run (FAR-604).

    Latest-wins queue coalescing: when a *pending* run for the same
    (pipeline, coalesce key) already exists, its input payload is UPDATED in
    place and ``created_at`` bumped (refreshing age-based sweeps and the
    backpressure window) instead of inserting a new row — Housekeeper-style
    re-dispatch churn (same PR re-delivered every 15 min) coalesces onto one
    pending run instead of ratcheting the queue. Returns the updated run, or
    ``None`` when no coalesce target exists (the caller inserts a fresh run).

    Only ``status='pending'`` rows are folded — a run that already started
    (or a terminal row) is never mutated. ``FOR UPDATE SKIP LOCKED`` on
    PostgreSQL: a run currently being dispatched is skipped and a fresh run
    is inserted (safe fallback, no blocked delivery). Non-PostgreSQL backends
    match the key in Python over a bounded candidate scan. The candidate
    match binds ``organisation_id`` explicitly (FAR-604 F7): on Postgres the
    GUC-pinned RLS context scopes the query, but the explicit org predicate
    means a fold can never cross tenants even if the session context is
    wrong — and on non-PostgreSQL backends (no RLS) it is the ONLY org
    guard.

    The stored payload is the strip + key-inject shape ``create_run`` would
    have persisted, and ``input_hash`` is recomputed from it.
    """
    stored_payload = _strip_reserved_keys(input_payload)
    stored_payload[_COALESCE_KEY_FIELD] = coalesce_key
    new_hash = _input_hash(stored_payload)

    dialect = await _get_dialect_name(session)
    if dialect == "postgresql":
        stmt = (
            select(Run)
            .where(
                Run.pipeline_id == pipeline_id,
                Run.status == "pending",
                Run.cancellation_requested.is_(False),
                Run.organisation_id == org_id,
                func.jsonb_extract_path_text(Run.input_payload, _COALESCE_KEY_FIELD) == coalesce_key,
            )
            .order_by(Run.created_at.desc())
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        run = (await session.execute(stmt)).scalar_one_or_none()
    else:
        candidates = (
            (
                await session.execute(
                    select(Run)
                    .where(
                        Run.pipeline_id == pipeline_id,
                        Run.status == "pending",
                        Run.cancellation_requested.is_(False),
                        Run.organisation_id == org_id,
                    )
                    .order_by(Run.created_at.desc())
                    .limit(_COALESCE_CANDIDATE_LIMIT)
                )
            )
            .scalars()
            .all()
        )
        run = next(
            (
                r
                for r in candidates
                if getattr(r, "organisation_id", None) == org_id and read_coalesce_key(r.input_payload) == coalesce_key
            ),
            None,
        )
    if run is None:
        return None
    run.input_payload = stored_payload
    run.input_hash = new_hash
    run.created_at = datetime.now(UTC)
    await session.flush()
    return run


async def get_pipeline_queue_depth(session: AsyncSession, pipeline_id: uuid.UUID) -> tuple[int, datetime | None]:
    """Return ``(pending_run_count, oldest_pending_created_at)`` for a pipeline.

    The queue-depth signal for the FAR-604 dispatcher backpressure gate:
    unstarted ``pending`` runs (cancellation-requested rows excluded, matching
    the active-run counters). ``(0, None)`` when the queue is empty.
    """
    stmt = (
        select(func.count(), func.min(Run.created_at))
        .select_from(Run)
        .where(
            Run.pipeline_id == pipeline_id,
            Run.status == "pending",
            Run.cancellation_requested.is_(False),
        )
    )
    row = (await session.execute(stmt)).one()
    return int(row[0] or 0), row[1]


async def get_run(session: AsyncSession, run_id: uuid.UUID, *, organisation_id: uuid.UUID | None = None) -> Run | None:
    """Fetch a single run by ID.

    Defence-in-depth: when *organisation_id* is provided, the query also
    filters on ``organisation_id`` so cross-tenant access is impossible even
    if RLS is misconfigured. RLS-based callers (routes that already call
    ``set_rls_org``) may omit it, but API-facing callers SHOULD pass it.
    """
    stmt = select(Run).where(Run.id == run_id)
    if organisation_id is not None:
        stmt = stmt.where(Run.organisation_id == organisation_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


# Heavy per-run payload columns that NO list consumer reads. Each is written by
# the executor (outputs, telemetry, compensation/classification records, sandbox
# dispatch state) and can carry megabytes per row on sandbox-heavy orgs, so the
# runs-list SELECT must omit them entirely (deferred columns are excluded from
# the emitted SQL — the DB never detoasts them for a 20-row page).
#
# Deliberately NOT deferred:
# * ``input_payload`` — masked into every REST list item (runs.py _build_list_item).
#
# FAR-583 (B2c): the three runs blob columns (outputs_json / node_telemetry_json /
# raw_output_markers) are NOT part of this ORM's select surface (B1 cut the ORM
# mapping; the drop migration 0215 removed the columns from the DB);
# the run_node_outputs store carries the payloads now.
#
# ``cost_breakdown`` IS deferred. Its only list-path reader is the MCP
# ``modulo://pipelines/{id}/runs`` resource (mcp_server._format_run_line), which
# now loads it through ``get_run_cost_breakdowns`` — ONE awaited query keyed by
# run id. Note a plain attribute read of a deferred column is never viable here:
# under asyncio the implicit lazy load raises ``MissingGreenlet`` even while the
# session is open, so the value must be loaded by an awaited query (or
# ``session.refresh``), not merely read "before the session closes".
# Every consumer of ``list_runs`` (REST runs route, MCP list_runs tool, MCP
# pipeline-runs resource, viewmodel RunSummary) was audited against this tuple —
# test_runs_list_query_deferral.py pins it.
_RUNS_LIST_DEFERRED_COLUMNS: tuple[str, ...] = (
    "node_token_usage",
    "cost_breakdown",
    "run_classification",
    "blocked_partial_summary",
    "guardrail_summary_json",
    "work_item_refs",
    "variant_config_snapshot",
    "sandbox_dispatch_state",
)


async def list_runs(
    session: AsyncSession,
    *,
    organisation_id: uuid.UUID | None = None,
    pipeline_id: uuid.UUID | None = None,
    status: str | None = None,
    trigger_type: str | None = None,
    search: str | None = None,
    page: int = 1,
    page_size: int = 20,
    cursor: str | None = None,
    team_id: uuid.UUID | None = None,
    variant_group_id: uuid.UUID | None = None,
    batch_id: uuid.UUID | None = None,
) -> PageResult[Run]:
    """List runs with optional org-scoped filtering.

    Defence-in-depth: when *organisation_id* is provided, the query also
    filters on ``organisation_id`` so cross-tenant access is impossible even
    if RLS is misconfigured. ``variant_group_id`` and ``batch_id`` narrow to
    a variant group's runs or a single fired batch (FAR-332 3e).

    Heavy payload columns (see ``_RUNS_LIST_DEFERRED_COLUMNS``) are deferred so
    a page SELECT never detoasts megabyte-scale JSONB the responses never read.
    """
    q = (
        select(Run)
        .options(
            selectinload(Run.pipeline),
            *(defer(getattr(Run, name)) for name in _RUNS_LIST_DEFERRED_COLUMNS),
        )
        .join(Pipeline, Run.pipeline_id == Pipeline.id, isouter=False)
        .where(Pipeline.deleted_at.is_(None))
    )
    count_q = (
        select(func.count())
        .select_from(Run)
        .join(Pipeline, Run.pipeline_id == Pipeline.id, isouter=False)
        .where(Pipeline.deleted_at.is_(None))
    )
    if organisation_id is not None:
        q = q.where(Run.organisation_id == organisation_id)
        count_q = count_q.where(Run.organisation_id == organisation_id)
    if team_id is not None:
        # A team-scoped caller sees runs for its own team's pipelines plus
        # org-level pipelines (no owner team) — the same boundary the MCP
        # guard applies. The run's stamped owner is the source of truth;
        # runs predating the create-time stamp (NULL) fall back to the
        # pipeline's owner so a NULL stamp can never widen the boundary.
        effective_owner = func.coalesce(Run.owner_team_id, Pipeline.owner_team_id)
        team_scope = team_scope_clause(effective_owner, team_id)
        q = q.where(team_scope)
        count_q = count_q.where(team_scope)
    if pipeline_id is not None:
        q = q.where(Run.pipeline_id == pipeline_id)
        count_q = count_q.where(Run.pipeline_id == pipeline_id)
    if status is not None:
        q = q.where(Run.status == status)
        count_q = count_q.where(Run.status == status)
    if trigger_type is not None:
        q = q.where(Run.trigger_type == trigger_type)
        count_q = count_q.where(Run.trigger_type == trigger_type)
    if search is not None:
        escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        q = q.where(Pipeline.name.ilike(f"%{escaped}%", escape="\\"))
        count_q = count_q.where(Pipeline.name.ilike(f"%{escaped}%", escape="\\"))
    if variant_group_id is not None:
        q = q.where(Run.variant_group_id == variant_group_id)
        count_q = count_q.where(Run.variant_group_id == variant_group_id)
    if batch_id is not None:
        q = q.where(Run.batch_id == batch_id)
        count_q = count_q.where(Run.batch_id == batch_id)

    if cursor is not None:
        paginator = CursorPaginator()
        cp = await paginator.paginate(
            session,
            q,
            cursor=cursor,
            limit=page_size,
            model=Run,
            compute_total=True,
        )
        return PageResult(
            items=cp.items,
            total=cp.total or 0,
            page=page,
            page_size=page_size,
            next_cursor=cp.next_cursor,
            has_more=cp.has_more,
        )

    offset = (page - 1) * page_size
    try:
        total = (await session.execute(count_q)).scalar_one_or_none() or 0
    except ProgrammingError:
        return PageResult(items=[], total=0, page=page, page_size=page_size)
    items = list((await session.execute(q.order_by(Run.created_at.desc()).offset(offset).limit(page_size))).scalars())
    return PageResult(items=items, total=total, page=page, page_size=page_size)


_COST_ROLLUP_QUANTUM = Decimal("0.000001")


async def get_child_run_rollup(
    session: AsyncSession,
    parent_run_ids: list[uuid.UUID],
) -> dict[uuid.UUID, tuple[Decimal, int]]:
    """Roll up child-run cost AND count per parent run.

    ONE ``GROUP BY`` query returning ``{parent_run_id: (total_cost, count)}``
    so callers avoid N+1 aggregation over the runs list. Parents with no
    children -- or only NULL-cost children -- are absent from the dict; callers
    treat a missing key as ``(0, 0)``. NULL ``total_cost_usd`` children
    contribute 0 to the SUM. Cost values are quantized to 6 decimal places to
    match the ``Numeric(14, 6)`` column scale.
    """
    if not parent_run_ids:
        return {}
    result = await session.execute(
        select(
            Run.parent_run_id,
            func.coalesce(func.sum(Run.total_cost_usd), 0),
            func.count().label("child_count"),
        )
        .where(Run.parent_run_id.in_(parent_run_ids))
        .group_by(Run.parent_run_id)
    )
    rollup: dict[uuid.UUID, tuple[Decimal, int]] = {}
    for parent_id, cost, count in result.all():
        rollup[uuid.UUID(str(parent_id))] = (
            Decimal(str(cost)).quantize(_COST_ROLLUP_QUANTUM),
            int(count),
        )
    return rollup


async def get_run_cost_breakdowns(
    session: AsyncSession,
    run_ids: list[uuid.UUID],
) -> dict[uuid.UUID, Any]:
    """Fetch ``cost_breakdown`` for the given runs in ONE awaited query.

    ``cost_breakdown`` is deferred by :func:`list_runs`
    (``_RUNS_LIST_DEFERRED_COLUMNS``), so list-path consumers that need it must
    load it explicitly. A plain attribute read on a deferred column is NOT an
    option under asyncio: the implicit lazy load attempts IO outside a greenlet
    and raises ``MissingGreenlet`` — this happens even while the session is
    still open, so "read it before the session closes" does not help. Only an
    awaited load (this query, or ``session.refresh``) works.

    Returns ``{run_id: cost_breakdown}`` for every requested run, including
    runs whose breakdown is ``NULL`` (mapped to ``None``); callers treat a
    missing key as ``None``. ONE query for the whole page — never a per-row
    load (avoids N+1).
    """
    if not run_ids:
        return {}
    result = await session.execute(select(Run.id, Run.cost_breakdown).where(Run.id.in_(run_ids)))
    return {uuid.UUID(str(run_id)): breakdown for run_id, breakdown in result.all()}


_COST_BREAKDOWN_SENTINEL: Any = object()


def _json_bind(value: Any) -> str | bytes | None:
    """Serialize a JSON-typed fenced-write param for asyncpg binding.

    asyncpg's default ``json`` codec accepts only ``str``/``bytes`` — a raw
    dict/list bound to ``CAST(:p AS json)`` raises ``DataError``. The fenced
    statement casts the serialized string to json, so dicts/lists are encoded
    here (mirroring SQLAlchemy's ORM ``JSON`` type serialization) while
    already-serialized strings pass through unchanged.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        return value.decode()
    return json.dumps(value)


@dataclass
class _RunStatusUpdate:
    """Grouped optional field updates for a run-status write.

    Carried by :func:`update_run_status` into both the ORM path and the fenced
    variant so the status-write machinery never threads a dozen scalar params.
    ``cost_breakdown`` defaults to the ``_COST_BREAKDOWN_SENTINEL`` ("leave
    alone"); passing ``None`` writes an explicit NULL.
    """

    error_code: str | None = None
    error_detail: str | None = None
    total_tokens: int | None = None
    total_cost_usd: Decimal | None = None
    cost_breakdown: Any = _COST_BREAKDOWN_SENTINEL
    node_token_usage: dict[str, Any] | None = None
    outputs_json: dict[str, Any] | None = None
    node_telemetry_json: dict[str, Any] | None = None
    claimed_by: str | None = None
    clear_error_code: bool = False
    claim_token: str | None = None
    from_status: str | None = None
    not_status: str | None = None


async def _write_unclassified_classification(session: AsyncSession, run: Run) -> None:
    """Fail-closed marker write for a terminal run the classifier could not process.

    Called when importing/calling the classifier itself failed — the classifier
    module may be the very thing that raised, so this write is fully
    self-contained (no ``classify`` import). A terminal run must NEVER commit
    with ``run_classification = NULL`` (a missing record breaks the FAR-190
    walk); the ``unclassified`` marker is what keeps the walk alive.
    Best-effort and NEVER raises.
    """
    try:
        await session.execute(
            update(Run)
            .where(Run.id == run.id)
            .values(
                run_classification={
                    "value": "unclassified",
                    "reason": "classifier_error",
                    "delivered_pr_urls": [],
                    "computed_at": datetime.now(UTC).isoformat(),
                    "work_intact": None,
                    "declared_success_nodes": 0,
                }
            )
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("classification.marker_fallback_failed run=%s", run.id)


async def _classify_terminal_run(session: AsyncSession, run: Run) -> None:
    """FAR-189 hook: classify + persist the run-outcome record for a terminal run.

    Best-effort and NEVER raises. Imported lazily so the classification
    machinery stays off the hot CRUD import path (the vast majority of
    ``update_run_status`` calls are non-terminal). The import AND the call are
    guarded: a classifier import failure must not roll back the terminal status
    write that already flushed — on any failure an ``unclassified`` marker is
    written directly instead. All callers gate on ``TERMINAL_STATUSES`` before
    invoking, so the status guard lives only in
    ``classify_and_persist_run`` (the shared entry point).
    """
    try:
        from modulo.core.pipeline_engine.classify import classify_and_persist_run

        await classify_and_persist_run(session, run)
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("classification.import_or_call_failed run=%s", run.id)
        await _write_unclassified_classification(session, run)


def _apply_run_claim_fields(run: Run, status: str, update: _RunStatusUpdate) -> None:
    """Stamp the lifecycle timestamps + claim driven by the new status."""
    if status == "running" and run.started_at is None:
        run.started_at = datetime.now(UTC)
    if update.claimed_by is not None:
        run.claimed_by = update.claimed_by
    if _is_terminal_status(status):
        run.completed_at = datetime.now(UTC)


def _apply_run_error_fields(run: Run, update: _RunStatusUpdate) -> None:
    """Apply the error marker fields (an explicit clear beats a new marker)."""
    if update.clear_error_code:
        # Explicitly clear a prior capacity marker (the error_code=... writes
        # below are conditional on non-None, so None alone cannot clear it).
        run.error_code = None
        run.error_detail = None
    if update.error_code is not None:
        run.error_code = update.error_code
    if update.error_detail is not None:
        run.error_detail = update.error_detail


def _apply_run_cost_fields(run: Run, update: _RunStatusUpdate) -> None:
    """Apply the cost fields (sentinel = leave cost_breakdown alone)."""
    if update.total_tokens is not None:
        run.total_tokens = update.total_tokens
    if update.total_cost_usd is not None:
        run.total_cost_usd = update.total_cost_usd
    if update.cost_breakdown is not _COST_BREAKDOWN_SENTINEL:
        # The eval_failed direct write PRESERVES the terminal field set: it
        # sets status + completed_at and leaves the cost fields untouched (the
        # eval pipeline never passes the cost kwargs). Passing the sentinel
        # (the default) means "leave cost_breakdown alone"; passing None writes
        # an explicit NULL (the pre-component-read terminal transition).
        run.cost_breakdown = update.cost_breakdown


def _apply_run_output_fields(run: Run, update: _RunStatusUpdate) -> None:
    """Apply the token-usage payload.

    FAR-583 B2c: the outputs/telemetry payloads have NO whole-run storage on
    ``runs`` (B1 cut the ORM mapping; the drop migration 0215 removed the
    follow-up PR - the incoming dicts are persisted on
    ``run_node_outputs`` by the primary repo write the caller performs right
    after (:func:`write_run_outputs_from_run`).
    """
    if update.node_token_usage is not None:
        run.node_token_usage = update.node_token_usage


# ---------------------------------------------------------------------------
# FAR-583 run-node-outputs primary-write chokepoint support (B1 contract cut)
# ---------------------------------------------------------------------------

# Retryable SQLSTATEs for the ONE bounded in-session retry of the new-table
# leg (qa iteration-1 correction): ONLY the statement timeout (57014) is
# genuinely savepoint-recoverable — the transaction-aborting states (40001
# serialization failure, 40P01 deadlock, 53300 connection exhaustion) abort
# the WHOLE transaction on Postgres, so re-entering a savepoint after catching
# them fails with 25P02 (a doomed extra round-trip); they go STRAIGHT to
# DualWriteError (the caller's full rollback + orchestration is the correct
# recovery for them). Hard errors (42501 RLS, 23503 FK, 23505 unique) also
# fail immediately. qa iteration 2: hoisted to the shared leaf
# :mod:`modulo.db.sqlstates` (the node-runner marker classification consumes
# the same vocabulary module); imported, never forked.
_DUAL_WRITE_RETRYABLE_SQLSTATES = DUAL_WRITE_RETRYABLE_SQLSTATES

# qa iteration 2 (Major 2): the SQLSTATE extractor lives in the shared leaf
# :mod:`modulo.db.sqlstates` — the bounded ``__context__`` walk is what keeps
# a savepoint's 25P02 rollback-wrapper failure from masking the ORIGINAL
# error's SQLSTATE (40P01 etc.) as DualWriteError.sqlstate.
_sqlstate_of = sqlstate_of


async def write_run_outputs_from_run(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    organisation_id: uuid.UUID | None,
    outputs: dict[str, Any] | None,
    telemetry: dict[str, Any] | None,
    claim_token: str | None = None,
    origin: str = "update_run_status",
    inherited_outputs: dict[str, Any] | None = None,
    inherited_telemetry: dict[str, Any] | None = None,
) -> None:
    """The PRIMARY repo store write of a run's outputs/telemetry blobs.

    B1 contract cut (FAR-583): this is not a dual-write. And B2c the writers
    no longer touch the ``runs`` blob columns at all (their SET clauses are
    absent from the fenced UPDATE as well; the DB columns were removed by
    the drop migration 0215) - the
    ``run_node_outputs`` REPLACE write
    (:func:`replace_run_node_outputs` - upsert ``__final__``/metadata rows
    + delete-absent ordered AFTER upserts, metadata flags re-derived) inside
    a SAVEPOINT in the caller's transaction is the ONLY store, and it FAILS
    LOUDLY when it fails. The pre-B1 name was
    ``dual_write_run_node_outputs``; renamed to reflect primacy.

    FAR-583-ERA semantics that survive the cut:

    * ``inherited_outputs`` / ``inherited_telemetry`` still carry the
      caller-captured PRE-WRITE stored dicts (qa M19 - read from the
      CURRENT new-table state since B2c) so inherited ``__``-prefixed
      keys are filtered from the store write instead of raising (their
      rows SURVIVE the REPLACE).
    * On store failure: ONE bounded in-session retry for the retryable
      SQLSTATEs (statement timeout only — see
      ``_DUAL_WRITE_RETRYABLE_SQLSTATES``); a transaction-aborting or hard
      error (or a second failure) raises :class:`DualWriteError` with the
      savepoint rolled back — the caller's transaction is left
      intact-but-poisoned so ITS rollback completes and the run is never
      half-written (the core-side orchestrator terminalizes the run and
      re-raises past the caller's transaction).
    * The former org-less silent skip is GONE: with the store the only
      holder of the payloads, a silently skipped store write would be data loss. An
      ORG-CONTEXT failure raises :class:`DualWriteError` too (qa iteration 1
      Major 1): a session with NO RLS org context or a MISMATCHED one is
      wrapped as ``DualWriteError(..., origin='rls_precheck')`` (carrying both
      orgs in the message) so every chokepoint failure shape flows through the
      SAME catch/rollback/orchestrate contract — an un-orchestrated
      ``OutputsRlsMismatch`` escape (no terminalize, no event) would violate
      the guard obligation below. The raw
      :class:`OutputsRlsMismatch` is chained as ``__cause__`` for diagnostics.
    * The kill-switch is GONE (removed with its machinery at B2a): there is
      no emergency OFF for this write - a failure surfaces through the
      loud abort alone.
    """
    if outputs is None and telemetry is None:
        return

    session_org = await read_rls_org(session)
    # qa iteration 1 (Major 1): the org-context precheck failures are wrapped
    # as DualWriteError (origin='rls_precheck') so the guardDualWrite-style
    # catch/rollback/orchestrate contract covers them exactly like an
    # in-savepoint store failure. The OutputsRlsMismatch is chained as
    # __cause__ for diagnostics; both orgs are carried in the message.
    if session_org is None:
        raise DualWriteError(
            f"run_node_outputs store write requires a bound RLS organisation context "
            f"(origin={origin}); wrap the call in an org-bound transaction",
            run_id=run_id,
            organisation_id=organisation_id,
            claim_token=claim_token,
            sqlstate=None,
            origin="rls_precheck",
        ) from OutputsRlsMismatch(
            "run_node_outputs store write requires a bound RLS organisation context; "
            "wrap the call in `async with session.begin():` + set_rls_org(session, org)"
        )
    if organisation_id is None:
        organisation_id = session_org
    if session_org != organisation_id:
        raise DualWriteError(
            f"run_node_outputs org mismatch on run {run_id}: session org {session_org} != row org "
            f"{organisation_id} (origin={origin})",
            run_id=run_id,
            organisation_id=organisation_id,
            claim_token=claim_token,
            sqlstate=None,
            origin="rls_precheck",
        ) from OutputsRlsMismatch(
            f"run_node_outputs org mismatch on run {run_id}: session org {session_org} != row org {organisation_id}"
        )

    for attempt in (1, 2):
        savepoint = session.begin_nested()
        try:
            async with savepoint:
                await replace_run_node_outputs(
                    session,
                    run_id=run_id,
                    organisation_id=organisation_id,
                    outputs=outputs,
                    telemetry=telemetry,
                    inherited_outputs=inherited_outputs,
                    inherited_telemetry=inherited_telemetry,
                )
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            state = _sqlstate_of(exc) if isinstance(exc, SQLAlchemyError) else None
            if attempt == 1 and state in _DUAL_WRITE_RETRYABLE_SQLSTATES:
                continue
            raise DualWriteError(
                f"run_node_outputs store write failed (origin={origin}, sqlstate={state}): {exc}",
                run_id=run_id,
                organisation_id=organisation_id,
                claim_token=claim_token,
                sqlstate=state,
                origin=origin,
            ) from exc


async def update_run_status(
    session: AsyncSession,
    run_id: uuid.UUID,
    status: str,
    *,
    error_code: str | None = None,
    error_detail: str | None = None,
    total_tokens: int | None = None,
    total_cost_usd: Decimal | None = None,
    cost_breakdown: Any = _COST_BREAKDOWN_SENTINEL,
    node_token_usage: dict[str, Any] | None = None,
    outputs_json: dict[str, Any] | None = None,
    node_telemetry_json: dict[str, Any] | None = None,
    claimed_by: str | None = None,
    clear_error_code: bool = False,
    claim_token: str | None = None,
    from_status: str | None = None,
    not_status: str | None = None,
) -> Run | None:
    """Write a run's status (optionally + blobs) — the FAR-583 store-write
    chokepoint.

    When *outputs_json* / *node_telemetry_json* are passed (the blobs kwargs
    keep their historical names for API stability), the payloads are persisted
    on ``run_node_outputs`` INSIDE THIS transaction by the primary repo write
    (:func:`write_run_outputs_from_run` — B1 contract cut: the legacy ``runs``
    blob columns are no longer written and their ORM mapping is gone) — which
    makes this function raise
    :class:`~modulo.db.crud.run_node_outputs.DualWriteError` when the
    store leg fails (qa M16 guard obligation): the caller's transaction
    must be rolled back cleanly (fail-closed abort) and the run terminalized
    ``dual_write_failed`` by the core orchestrator.

    GUARD OBLIGATION — every blobs-passing call site MUST wrap the call in
    :func:`modulo.core.run_outputs_dualwrite.guard_dual_write` (catch →
    rollback → orchestrate → re-raise). The sanctioned wrapped sites:

    * ``core.cost_controller.finalize._write_finalized_run`` (the single
      finalization write);
    * ``core.cost_controller.finalize._fallback_write`` (the legacy fallback
      branch);
    * ``core.cost_controller.finalize._reduced_escape`` (the reduced escape —
      blobs arrive via ``**finalize_fields``).

    Sites passing NO blobs (``core.dispatch._org_capacity_deferred``, the
    ``executor`` claim/capacity/ceiling writes, ``api.routes.hitl.claim_gate``,
    ``finalize._write_empty_terminal``) cannot raise DualWriteError and need
    no guard. A new blobs-passing call site added without the guard aborts
    un-orchestrated (no terminalize, no event) — the
    ``test_run_outputs_gate`` architecture pin fails it.
    """
    if status not in RUN_STATUS_WHITELIST:
        raise ValueError(f"invalid run status: {status!r}")
    update = _RunStatusUpdate(
        error_code=error_code,
        error_detail=error_detail,
        total_tokens=total_tokens,
        total_cost_usd=total_cost_usd,
        cost_breakdown=cost_breakdown,
        node_token_usage=node_token_usage,
        outputs_json=outputs_json,
        node_telemetry_json=node_telemetry_json,
        claimed_by=claimed_by,
        clear_error_code=clear_error_code,
        claim_token=claim_token,
        from_status=from_status,
        not_status=not_status,
    )
    if update.claim_token is not None:
        return await _update_run_status_fenced(session, run_id, status, update=update)
    result = await session.execute(select(Run).where(Run.id == run_id).with_for_update())
    run = result.scalar_one_or_none()
    if run is None:
        return None
    # Guarded write (qa F7): when *not_status* is set and the row is currently
    # in that status, the write is SKIPPED (logged) instead of applied. The
    # read-then-write here is serialized by the FOR UPDATE row lock, so the
    # guard is evaluated against the authoritative row state — a concurrent
    # writer (e.g. the park sweep parking the run mid-claim) either committed
    # before this lock was taken (guard sees it) or waits behind it.
    if update.not_status is not None and run.status == update.not_status:
        _log.info(
            "run_status.guard_skipped run=%s requested=%s forbidden_from=%s — write not applied",
            run.id,
            status,
            update.not_status,
        )
        return run
    run.status = status
    # FAR-583 qa-M19 (B2c): capture the PRE-WRITE stored blob dicts BEFORE — the primary store write filters inherited
    # ``__``-prefixed keys against exactly these (B1-era legacy rows carry
    # sentinels on the legacy column; the drop migration 0215's repair
    # re-maps them into the new table — they never reach the new-table
    # capture) so an
    # inherited sentinel id can never wedge the run's terminalization.
    # B2c: the capture reads the CURRENT new-table state via the repo reader — one extra SELECT, only when the
    # payload actually carries blobs (same shape as the fenced branch).
    pre_write_outputs: Any = None
    pre_write_telemetry: Any = None
    if update.outputs_json is not None or update.node_telemetry_json is not None:
        # getattr mirrors the store write below (a fake Run stand-in in unit
        # tests carries no organisation_id; an org-less capture reads across
        # the row set the reader's tenant compensation covers).
        # B2c: the capture reads the CURRENT new-table state (readers are
        # new-table-only; the runs blob columns are unwritten since B1 and
        # were dropped by migration 0215; the inherited-sentinel filter
        # speaks new-table
        # row ids now).
        stored = await read_run_node_outputs_raw(
            session, run_id=run_id, organisation_id=getattr(run, "organisation_id", None)
        )
        pre_write_outputs = stored.outputs
        pre_write_telemetry = stored.telemetry
    _apply_run_claim_fields(run, status, update)
    _apply_run_error_fields(run, update)
    _apply_run_cost_fields(run, update)
    _apply_run_output_fields(run, update)
    await session.flush()
    # FAR-583 B1 primary write: when the payload carries outputs/telemetry the
    # run_node_outputs REPLACE leg is the ONLY store (fail-loud). A failure
    # raises DualWriteError — the caller's rollback completes cleanly and the
    # core orchestrator terminalizes the run (fail-closed abort).
    await write_run_outputs_from_run(
        session,
        run_id=run_id,
        # getattr: a fake Run stand-in (unit tests) carries no organisation_id;
        # the helper resolves it from the session's RLS org (raises
        # fail-closed when the session has none — the org-less skip is gone).
        organisation_id=getattr(run, "organisation_id", None),
        outputs=update.outputs_json,
        telemetry=update.node_telemetry_json,
        claim_token=update.claim_token,
        origin="update_run_status.orm",
        inherited_outputs=pre_write_outputs if isinstance(pre_write_outputs, dict) else None,
        inherited_telemetry=pre_write_telemetry if isinstance(pre_write_telemetry, dict) else None,
    )
    if run.status in TERMINAL_STATUSES:
        await _classify_terminal_run(session, run)
    return run


_UPDATE_STATUS_FENCED_SQL = text(
    "UPDATE runs SET "
    "status = CASE "
    "  WHEN cancellation_requested AND :status IN ('awaiting_human', 'complete') THEN 'cancelled' "
    "  ELSE :status END, "
    "started_at = CASE WHEN :status = 'running' AND started_at IS NULL THEN now() ELSE started_at END, "
    "completed_at = CASE "
    "  WHEN cancellation_requested AND :status IN ('awaiting_human', 'complete') THEN now() "
    "  WHEN :status IN ('complete', 'failed', 'cancelled', 'eval_failed', 'stalled', "
    "'budget_exceeded', 'router_no_match', 'cost_ceiling_exceeded', 'compensation_failed') THEN now() "
    "  ELSE completed_at END, "
    "claimed_by = CASE WHEN CAST(:claimed_by AS text) IS NOT NULL THEN CAST(:claimed_by AS text) ELSE claimed_by END, "
    "error_code = CASE WHEN :clear_error_code THEN NULL "
    "  ELSE COALESCE(CAST(:error_code AS text), error_code) END, "
    "error_detail = CASE WHEN :clear_error_code THEN NULL "
    "  WHEN CAST(:error_code AS text) IS NOT NULL THEN CAST(:error_detail AS text) ELSE error_detail END, "
    "total_tokens = COALESCE(:total_tokens, total_tokens), "
    "total_cost_usd = COALESCE(:total_cost_usd, total_cost_usd), "
    "cost_breakdown = CASE WHEN :cost_breakdown_sentinel THEN CAST(cost_breakdown AS jsonb) "
    "  ELSE CAST(:cost_breakdown AS jsonb) END, "
    "node_token_usage = CASE WHEN CAST(:node_token_usage AS jsonb) IS NOT NULL "
    "  THEN CAST(:node_token_usage AS jsonb) ELSE CAST(node_token_usage AS jsonb) END "
    "WHERE id=:rid "
    "AND (CAST(:tok AS text) IS NULL OR claim_token = CAST(:tok AS text)) "
    "AND (CAST(:from_status AS text) IS NULL OR status = CAST(:from_status AS text)) "
    "AND (CAST(:not_status AS text) IS NULL OR status <> CAST(:not_status AS text)) "
    "AND (cancellation_requested = false OR :status IN ('cancelled', 'awaiting_human', 'complete')) "
    "RETURNING id"
)


async def _update_run_status_fenced(
    session: AsyncSession,
    run_id: uuid.UUID,
    status: str,
    *,
    update: _RunStatusUpdate,
) -> Run | None:
    """Fenced variant of :func:`update_run_status` (dist/runtime-core A1).

    A single conditional UPDATE guarded by ``claim_token = :tok`` (a superseded
    executor cannot terminalize the run out from under a successor), an optional
    ``status = :from_status`` source-state constraint (used by the capacity
    demotion), and CANCEL-WINS precedence (``cancellation_requested = false``
    unless the write is a ``cancelled`` write). An ``awaiting_human``/``complete``
    write against a cancellation-requested row is rewritten to ``cancelled`` in
    the same statement. Returns the refreshed Run row, or ``None`` when the
    guards rejected the write (superseded / wrong source state /
    cancelled-and-not-a-cancel-write / missing).
    """
    # FAR-583 qa-M19: the PRE-WRITE legacy blob dicts for the fenced branch
    # are captured AFTER the fenced UPDATE (just below, next to the store
    # write, where *refreshed_run* carries the row's organisation_id): the
    # fenced UPDATE's SET clauses never touch the blob stores, so the
    # capture is pre-write-equivalent while still binding the tenant
    # predicate the raw reader compensates with. Only when the payload
    # actually carries blobs — a fenced write without outputs/telemetry never
    # needs the pre-state.
    result = await session.execute(
        _UPDATE_STATUS_FENCED_SQL,
        {
            "status": status,
            "rid": str(run_id),
            "tok": update.claim_token,
            "from_status": update.from_status,
            "not_status": update.not_status,
            "error_code": update.error_code,
            "error_detail": update.error_detail,
            "total_tokens": update.total_tokens,
            "total_cost_usd": update.total_cost_usd,
            "cost_breakdown_sentinel": update.cost_breakdown is _COST_BREAKDOWN_SENTINEL,
            # When the sentinel is used the ELSE branch is never taken, but the
            # parameter still must be bindable (NULL json) — never the sentinel
            # object itself. JSON-typed params are serialized via ``_json_bind``:
            # asyncpg's json codec rejects raw dict/list (DataError), so the
            # fenced terminal write must encode them exactly like the ORM path.
            "cost_breakdown": (
                None if update.cost_breakdown is _COST_BREAKDOWN_SENTINEL else _json_bind(update.cost_breakdown)
            ),
            "node_token_usage": _json_bind(update.node_token_usage),
            "claimed_by": update.claimed_by,
            "clear_error_code": update.clear_error_code,
        },
    )
    if result.fetchone() is None:
        return None
    # ``populate_existing`` forces a REAL row read rather than returning the
    # session's identity-map object (which is STALE for status/error_code/
    # outputs_json after the raw fenced UPDATE above — finalize_cost loads the
    # run earlier in the same transaction). The classification hook needs the
    # freshly-written facts.
    refreshed = await session.execute(select(Run).where(Run.id == run_id).execution_options(populate_existing=True))
    refreshed_run = refreshed.scalar_one_or_none()
    if refreshed_run is None:
        return None
    # The pre-write capture (FAR-583 qa-M19, B2c): the inherited-key filter
    # compares against the CURRENT new-table state; the tenant predicate
    # binds the row's org.
    pre_write_outputs: Any = None
    pre_write_telemetry: Any = None
    if update.outputs_json is not None or update.node_telemetry_json is not None:
        # B2c: the capture reads the CURRENT new-table state (see the ORM
        # branch comment).
        stored = await read_run_node_outputs_raw(session, run_id=run_id, organisation_id=refreshed_run.organisation_id)
        pre_write_outputs = stored.outputs
        pre_write_telemetry = stored.telemetry
    # FAR-583 B1 primary write: fenced branch — the REPLACE leg runs ONLY when
    # the fence accepted the write (rowcount 1, checked above). The retry +
    # fail-loud contract is the shared helper's; a DualWriteError here rolls
    # back the fenced status write together with the store leg (the run row
    # is never half-written). The legacy blob columns are no longer written.
    await write_run_outputs_from_run(
        session,
        run_id=run_id,
        organisation_id=refreshed_run.organisation_id,
        outputs=update.outputs_json,
        telemetry=update.node_telemetry_json,
        claim_token=update.claim_token,
        origin="update_run_status.fenced",
        inherited_outputs=pre_write_outputs if isinstance(pre_write_outputs, dict) else None,
        inherited_telemetry=pre_write_telemetry if isinstance(pre_write_telemetry, dict) else None,
    )
    if refreshed_run.status in TERMINAL_STATUSES:
        await _classify_terminal_run(session, refreshed_run)
    return refreshed_run


async def unpark_parked_run(session: AsyncSession, *, run_id: uuid.UUID, org_id: uuid.UUID) -> None:
    """Un-park a ``hitl_parked`` run back to ``awaiting_human`` (qa F15).

    The single shared un-park transition (FAR-604 D3): a parked run re-enters
    normal admission the moment its gate is decided — by
    ``HITLManager._decide`` (approve/reject/deliver-manual) or by the gate
    coalescing's supersede close-out (the old gate auto-rejected). The
    predicate is guarded to ``status = 'hitl_parked'`` ONLY, so it is a no-op
    for every non-parked run and can never clobber a concurrent
    running/claimed transition. Parked runs resumed by the dispatcher
    reconcile instead go through ``resume_run`` (which claims the row).
    """
    await session.execute(
        update(Run)
        .where(
            Run.id == run_id,
            Run.organisation_id == org_id,
            Run.status == HITL_PARKED_STATUS,
        )
        .values(status=AWAITING_HUMAN_STATUS)
    )


_TRANSITION_SQL = text(
    "UPDATE runs SET status=CAST(:target AS text), "
    "completed_at = CASE WHEN CAST(:target AS text) IN "
    "('complete', 'failed', 'cancelled', 'eval_failed', 'stalled', 'budget_exceeded', 'router_no_match', "
    "'cost_ceiling_exceeded', 'compensation_failed') "
    "THEN now() ELSE completed_at END, "
    "error_code = COALESCE(CAST(:error_code AS text), error_code), "
    "error_detail = CASE WHEN CAST(:error_code AS text) IS NOT NULL "
    "THEN CAST(:error_detail AS text) ELSE error_detail END "
    "WHERE id=:rid AND organisation_id=:oid "
    "AND status IN :allowed_from "
    "AND (CAST(:tok AS text) IS NULL OR claim_token = CAST(:tok AS text)) "
    "AND cancellation_requested = false "
    "RETURNING id"
).bindparams(bindparam("allowed_from", expanding=True))


async def transition_run(
    session: AsyncSession,
    run_id: uuid.UUID,
    org_id: uuid.UUID,
    *,
    target_status: str,
    error_code: str | None = None,
    error_detail: str | None = None,
    claim_token: str | None = None,
    allowed_from: frozenset[str] | None = None,
) -> bool:
    """The primary fenced run-transition authority (dist/runtime-core A1).

    The single fenced run-transition authority for the REST/executor paths —
    ``saq_hooks._mark_run_failed`` is a deliberate SECOND fenced authority for
    the guarded SAQ task_failure path (PR #1003), so this is the primary, not
    the only, fence.

    Performs ONE conditional ``UPDATE ... WHERE ... RETURNING id`` that is safe
    under concurrency:

    * ``status IN (allowed_from)`` — the transition only applies when the row
      is currently in an admissible source state (terminal writes pass the
      non-terminal states).
    * ``(:tok IS NULL OR claim_token = :tok)`` — when *claim_token* is given the
      write is FENCED to the claim that owns the run; a superseded executor's
      token no longer matches and the write is a no-op (rowcount 0).
    * ``cancellation_requested = false`` — CANCEL-WINS precedence: once a
      cancellation is requested, no non-cancelled writer can transition the row;
      only the cancel path (which sets ``cancellation_requested``) may write
      ``cancelled``.

    ``completed_at`` is stamped only for terminal targets; ``error_code`` /
    ``error_detail`` are written only when *error_code* is provided (an explicit
    ``None`` never clears a prior marker — callers use the ``clear_error_code``
    path of :func:`update_run_status` for that).

    Returns ``True`` when exactly one row was transitioned (``RETURNING id``
    yielded a row), ``False`` when the guards rejected the write (superseded /
    wrong source state / cancellation requested / row missing).

    RLS org context must be set by the caller (all ``db.crud.run`` functions
    require it).
    """
    if target_status not in RUN_STATUS_WHITELIST:
        raise ValueError(f"invalid run status: {target_status!r}")
    if allowed_from is None:
        allowed_from = RUN_STATUS_WHITELIST
    result = await session.execute(
        _TRANSITION_SQL,
        {
            "target": target_status,
            "rid": str(run_id),
            "oid": str(org_id),
            "error_code": error_code,
            "error_detail": error_detail,
            "tok": claim_token,
            "allowed_from": sorted(allowed_from),
        },
    )
    ok = result.fetchone() is not None
    if ok and target_status in TERMINAL_STATUSES:
        refreshed = await session.execute(select(Run).where(Run.id == run_id).execution_options(populate_existing=True))
        refreshed_run = refreshed.scalar_one_or_none()
        if refreshed_run is not None and refreshed_run.status in TERMINAL_STATUSES:
            await _classify_terminal_run(session, refreshed_run)
    return ok


async def request_cancellation(session: AsyncSession, run_id: uuid.UUID) -> Run | None:
    result = await session.execute(select(Run).where(Run.id == run_id).with_for_update())
    run = result.scalar_one_or_none()
    if run is None:
        return None
    run.cancellation_requested = True
    run.status = "cancelled"
    run.completed_at = datetime.now(UTC)
    await session.flush()
    if run.status in TERMINAL_STATUSES:
        await _classify_terminal_run(session, run)
    return run


# Active (non-terminal) run statuses — the canonical set defined ONCE in
# models.run (the never-entered ``waiting_for_lock`` sub-state was excised in
# migration 0074/0075). A pending run only counts when ``include_pending=True``
# is requested (variant-group quota); capacity gates pass
# ``include_pending=False`` because a pending run does not hold a slot.
_ACTIVE_RUN_STATUSES = ACTIVE_RUN_STATUSES


def _active_run_statuses(include_pending: bool) -> set[str]:
    """Resolve the status set for an active-run count (qa F14).

    * ``include_pending=True`` (quota): all non-terminal runs including
      ``pending``.
    * ``include_pending=False``: running/awaiting_human/claimed/unknown/
      hitl_parked — a pending run does not hold capacity. The pipeline gate
      additionally excludes the human-waiting statuses (FAR-604 D1); that
      scoping is selected inside :func:`_count_active_runs` (which already
      knows the gate's scope) instead of a required ``scope`` keyword here —
      the old required kwarg broke every two-argument caller/test.
    """
    if include_pending:
        return set(_ACTIVE_RUN_STATUSES)
    return set(_ACTIVE_RUN_STATUSES - {"pending"})


async def _count_active_runs(
    session: AsyncSession,
    *,
    org_id: uuid.UUID | None,
    pipeline_id: uuid.UUID | None,
    include_pending: bool,
    exclude_run_id: uuid.UUID | None,
) -> int:
    """Shared active-run counter for the pipeline- and org-scoped gates.

    Scopes to exactly one of *org_id* (org gate) or *pipeline_id* (pipeline
    gate). ``include_pending=True`` counts every non-terminal status (quota
    semantics); ``include_pending=False`` selects the capacity set — the
    pipeline gate excludes the human-waiting statuses (FAR-604 D1), the org
    gate does not. Optionally excludes a specific *run_id* so a pending run
    does not count itself when checking capacity.
    """
    # Pipeline scope (include_pending=False only) excludes the human-waiting
    # statuses (FAR-604 D1: a human decision may take days and must never
    # starve admission); the org scope keeps counting them (the org-wide
    # worker pool stays bounded). include_pending=True is the QUOTA semantics
    # and wins over the scope: every non-terminal status counts.
    if include_pending:
        statuses = _active_run_statuses(True)
    elif pipeline_id is not None:
        statuses = set(PIPELINE_CAPACITY_STATUSES)
    else:
        statuses = _active_run_statuses(False)
    stmt = (
        select(func.count())
        .select_from(Run)
        .where(
            Run.status.in_(statuses),
            Run.cancellation_requested.is_(False),
        )
    )
    if pipeline_id is not None:
        stmt = stmt.where(Run.pipeline_id == pipeline_id)
    elif org_id is not None:
        stmt = stmt.where(Run.organisation_id == org_id)
    if exclude_run_id is not None:
        stmt = stmt.where(Run.id != exclude_run_id)
    result = await session.execute(stmt)
    return int(result.scalar_one_or_none() or 0)


async def count_active_runs_for_pipeline(
    session: AsyncSession,
    pipeline_id: uuid.UUID,
    include_pending: bool,
    exclude_run_id: uuid.UUID | None = None,
) -> int:
    """Count active runs for a pipeline.

    ``include_pending`` selects the behaviour (plan F3b — two behaviours
    instead of three):

    * ``include_pending=False`` (capacity gate): counts only runs that are
      actually executing or claimed (running/claimed/unknown) — a pending run
      does not hold capacity, and NEITHER does a run parked on a human
      decision (``awaiting_human``/``hitl_parked`` are excluded — FAR-604 D1:
      a human decision may take days and must never starve the pipeline's
      admission; the 2026-09-04 incident had 20 awaiting_human runs consuming
      a 20-cap pipeline for 26h). The org-level gate still counts those runs.
    * ``include_pending=True`` (variant-group quota): counts all non-terminal
      runs including ``pending``, preserving the 429 quota semantics.

    Optionally excludes a specific *run_id* from the count so a pending run does
    not count itself when checking capacity.
    """
    return await _count_active_runs(
        session,
        org_id=None,
        pipeline_id=pipeline_id,
        include_pending=include_pending,
        exclude_run_id=exclude_run_id,
    )


async def count_active_runs_for_org(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    include_pending: bool,
    exclude_run_id: uuid.UUID | None = None,
) -> int:
    """Count active runs for an organisation (org-level run concurrency gate).

    Mirrors :func:`count_active_runs_for_pipeline` but scopes to the WHOLE org
    across all pipelines. ``include_pending`` selects the same two behaviours:

    * ``include_pending=False`` (dispatch admission gate): counts only runs
      that are actually executing or held (running/awaiting_human/claimed/
      unknown) — a pending run does not hold capacity. UNLIKE the pipeline
      gate, human-waiting runs (``awaiting_human``/``hitl_parked``) still
      count here: the org-wide worker pool is the global shared resource the
      org-level ``run_concurrency_limit`` exists to bound, and parked runs
      must not escape that bound (FAR-604 D1).
    * ``include_pending=True`` (quota semantics): counts all non-terminal runs
      including ``pending``.

    ``exclude_run_id`` lets a pending run avoid counting itself. The explicit
    ``organisation_id`` filter makes the query cross-tenant safe on top of RLS
    (like :func:`count_active_sandbox_runs_for_org`) — a caller must still set
    RLS org context before invoking.
    """
    return await _count_active_runs(
        session,
        org_id=org_id,
        pipeline_id=None,
        include_pending=include_pending,
        exclude_run_id=exclude_run_id,
    )


def _graph_contains_sandbox_agent(graph_json: dict[str, Any] | None) -> bool:
    """Top-level scan for any ``sandbox_agent`` node in a snapshot graph.

    Fail-open: ``None``, non-dicts, and missing ``nodes`` return ``False``
    (treat as non-sandbox, never block). Only the top-level ``nodes`` list is
    scanned — composite pipelines ARE compilable today: snapshots are expanded
    at creation time (``create_snapshot_from_live_graph``), so any sandbox
    sub-node of a composite template appears directly in the snapshot's
    top-level ``nodes`` and is found by this scan.
    """
    if not isinstance(graph_json, dict):
        return False
    nodes = graph_json.get("nodes")
    if not isinstance(nodes, list):
        return False
    return any(isinstance(n, dict) and n.get("node_type") == "sandbox_agent" for n in nodes)


async def count_active_sandbox_runs_for_org(
    session: AsyncSession,
    org_id: uuid.UUID,
    exclude_run_id: uuid.UUID | None = None,
) -> int:
    """Count ``running`` sandbox-agent runs for an organisation.

    Only ``running`` runs whose snapshot graph contains a ``sandbox_agent``
    node count against the org sandbox cap. It is the sole executing state;
    pending, awaiting_human, and claimed runs hold no live sandbox — and
    neither do non-sandbox pipelines, so they must not consume a slot (B5).
    The explicit ``organisation_id`` filter makes the query cross-tenant safe
    on top of RLS; the snapshots join runs under the same RLS context.
    """
    stmt = (
        select(PipelineSnapshot.graph_json)
        .join(Run, Run.snapshot_id == PipelineSnapshot.id)
        .where(
            Run.organisation_id == org_id,
            Run.status == "running",
            Run.cancellation_requested.is_(False),
        )
    )
    if exclude_run_id is not None:
        stmt = stmt.where(Run.id != exclude_run_id)
    rows = (await session.execute(stmt)).scalars()
    return sum(1 for graph_json in rows if _graph_contains_sandbox_agent(graph_json))


# ---------------------------------------------------------------------------
# Runner-dispatch marker SQL fragments (FAR-594 D8) — the SINGLE source.
#
# The DB layer owns the count body (core imports db, never the reverse), so
# these live here and every consumer — including core.runner_capacity's
# documentation of the vocabulary — references this module. Compile-time
# constants only: nothing user-controlled is ever interpolated.
#
# The tombstone exclusion matches the marker's ``state`` field PRECISELY
# (qa F13): a bare ``%cleared_at_hitl%`` substring would also exclude a
# marker whose attempt_key merely contains the literal. The provider
# attribution treats legacy tier-less markers as runner_docker (fail-safe —
# they can only be E2B-legacy or Docker, and Docker is the tier the
# default-cap bucket must not undercount).
# ---------------------------------------------------------------------------
RUNNER_TOMBSTONE_EXCLUSION_SQL = 'runs.sandbox_dispatch_state NOT LIKE \'%"state": "cleared_at_hitl"%\''
RUNNER_HOST_RESOURCE_FILTER_SQL = (
    "COALESCE("
    "CASE WHEN runs.sandbox_dispatch_state LIKE '%\"provider\"%' "
    "THEN substring(runs.sandbox_dispatch_state from "
    '\'"provider"[[:space:]]*:[[:space:]]*"([a-z_]+)"\') END, '
    "'runner_docker') IN ('runner_docker', 'local')"
)


async def count_active_runner_dispatches_for_org(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    exclude_run_id: uuid.UUID | None = None,
    host_resource_only: bool = False,
) -> int:
    """Count runs with a LIVE runner dispatch for concurrency tracking (FAR-296 Phase 4b).

    Renamed from ``count_active_sandbox_leases_for_org`` (FAR-587); the count
    is marker-backed — it counts runs whose ``sandbox_dispatch_state`` holds a
    live dispatch marker.

    The POPULATION is flag-dependent (qa F6 — the flag-off window keeps the
    pre-D8 behaviour EXACTLY):

    - Flag ON (D8): ``running`` runs ONLY — the only status a marker can
      legally exist on (the fenced marker UPDATE requires ``status =
      'running'``). ``awaiting_human``/``pending``/``claimed``/``unknown``/
      ``hitl_parked`` hold NO slot: a parked HITL run cannot starve the org,
      and the resume path re-acquires a slot when the HITL approval
      re-dispatches. Tombstoned markers (``cleared_at_hitl``) are excluded —
      the HITL-boundary tombstone is capacity-neutral.
    - Flag OFF: ``ACTIVE_RUN_STATUSES`` with NO tombstone exclusion — the
      exact pre-D8 population (the D8 tombstone never fires with the flag
      off, so there is nothing to exclude).

    The count EXCLUDES the run's own marker (``exclude_run_id``) so a
    re-dispatch of a run still carrying a fence-carrying stale marker cannot
    self-block.

    ``host_resource_only=True`` scopes the count to the host-resource
    providers (Docker + Local) for the absent-key Docker-tier default; the
    provider is attributed from the marker's JSON ``"provider"`` key, with
    legacy tier-less markers attributed to ``runner_docker`` (fail-safe — see
    ``modulo.core.runner_capacity``).
    """
    from modulo.settings import get_settings

    stmt = (
        select(func.count())
        .select_from(Run)
        .where(
            Run.organisation_id == org_id,
            Run.sandbox_dispatch_state.isnot(None),
        )
    )
    if exclude_run_id is not None:
        stmt = stmt.where(Run.id != exclude_run_id)
    if get_settings().runner_capacity_gate_enabled:
        # D8: running-only + tombstone exclusion (SQL fragments — the marker
        # is a Text column holding our own JSON vocabulary).
        stmt = stmt.where(Run.status == "running", text(RUNNER_TOMBSTONE_EXCLUSION_SQL))
    else:
        # Flag-off: the pre-D8 population exactly.
        stmt = stmt.where(Run.status.in_(ACTIVE_RUN_STATUSES))
    if host_resource_only:
        stmt = stmt.where(text(RUNNER_HOST_RESOURCE_FILTER_SQL))
    result = (await session.execute(stmt)).scalar_one()
    return int(result) if result is not None else 0


async def _read_org_int_limit(
    session: AsyncSession,
    org_id: uuid.UUID,
    key: str,
    min_value: int,
    max_value: int,
    log_prefix: str,
) -> int | None:
    """Read an org-level integer limit from ``settings_json`` (shared reader).

    ``None`` means no cap. Fail-open: a malformed value (non-dict settings,
    string, float, bool) or a missing org returns ``None`` with a warning and
    never raises. An out-of-range ``int`` is clamped to ``[min_value,
    max_value]`` so a direct-DB edit cannot crash the capacity check. The
    *log_prefix* selects the structured-log event namespace (e.g.
    ``sandbox_concurrency`` / ``run_concurrency``).
    """
    org = await get_organisation(session, org_id)
    if org is None:
        _log.warning(f"{log_prefix}.org_not_found", extra={"org_id": str(org_id)})
        return None
    settings = org.settings_json
    if not isinstance(settings, dict):
        _log.warning(f"{log_prefix}.settings_not_dict", extra={"org_id": str(org_id)})
        return None
    raw = settings.get(key)
    if raw is None:
        return None
    if not _is_valid_int_limit_value(raw):
        _log.warning(
            f"{log_prefix}.invalid_type",
            extra={"org_id": str(org_id), "value": repr(raw)},
        )
        return None
    if raw < min_value or raw > max_value:
        _log.warning(
            f"{log_prefix}.out_of_range",
            extra={"org_id": str(org_id), "value": raw},
        )
        return max(min_value, min(max_value, raw))
    return raw


@dataclass(frozen=True)
class SandboxConcurrencyLimit:
    """Resolved org sandbox/runner capacity contract (FAR-589 D3b).

    One reader, one contract, consumed by all five call sites (dispatch gate,
    HITL pre-check, claim-time cap read, resume gate, admin GET endpoint).
    ``cap`` is the resolved cap; ``is_default`` marks the ABSENT-key case
    (the Docker-tier default) so D8's gate can scope its provider-filtered
    count to it, and the admin UI can render "4 (default)".
    """

    cap: int | None
    is_default: bool = False

    @property
    def enforced_cap(self) -> int | None:
        """The cap the flag-off-window gates enforce.

        Flag-off + absent key means NO gate: the Docker-tier default
        (``is_default=True``) activates only with D8's short-lived rollout
        flag — the unfiltered racy count must never enforce default-4, or the
        rollout window silently caps the dogfood org's E2B workload at 4.
        Explicit values (including ``0`` deny-all) and explicit ``null``
        (no gate) enforce on every path.
        """
        if self.is_default:
            return None
        return self.cap


def _resolve_sandbox_concurrency_limit(settings: Any, org_id: uuid.UUID) -> SandboxConcurrencyLimit:
    """Pure resolver for the ``sandbox_concurrency_limit`` key (FAR-589 D3b).

    Distinguishes the previously-conflated ABSENT key (Docker-tier default 4,
    ``is_default=True``) from an explicit ``null`` (no gate). The clamp range
    drops the old ``_SANDBOX_CONCURRENCY_MIN = 1`` floor — ``0`` is now a
    meaningful value (deny-all) — so out-of-range values clamp to ``[0, 100]``.
    Fail-open: a non-dict settings blob or a non-int value resolves to no gate
    with a warning; never raises.
    """
    if not isinstance(settings, dict):
        _log.warning(
            "sandbox_concurrency.settings_not_dict",
            extra={"org_id": str(org_id), "value_type": type(settings).__name__},
        )
        return SandboxConcurrencyLimit(cap=None)
    if _SANDBOX_CONCURRENCY_KEY not in settings:
        return SandboxConcurrencyLimit(cap=_SANDBOX_CONCURRENCY_DEFAULT, is_default=True)
    raw = settings[_SANDBOX_CONCURRENCY_KEY]
    if raw is None:
        return SandboxConcurrencyLimit(cap=None)
    if not _is_valid_int_limit_value(raw):
        _log.warning(
            "sandbox_concurrency.invalid_type",
            extra={"org_id": str(org_id), "value": repr(raw)},
        )
        return SandboxConcurrencyLimit(cap=None)
    if raw < 0 or raw > _SANDBOX_CONCURRENCY_MAX:
        _log.warning(
            "sandbox_concurrency.out_of_range",
            extra={"org_id": str(org_id), "value": raw},
        )
        return SandboxConcurrencyLimit(cap=max(0, min(_SANDBOX_CONCURRENCY_MAX, raw)))
    return SandboxConcurrencyLimit(cap=raw)


async def get_sandbox_concurrency_limit(session: AsyncSession, org_id: uuid.UUID) -> "SandboxConcurrencyLimit":
    """Read the org's sandbox/runner concurrency contract from ``settings_json``.

    FAR-589 D3b semantics for the stored ``sandbox_concurrency_limit`` key
    (identifier frozen — no key rename, no data migration):

    - key ABSENT → Docker-tier default 4 (``is_default=True``)
    - explicit int → gates ALL runner dispatches; ``0`` = deny-all
    - explicit ``null`` → no gate
    - fail-open (missing org, non-dict settings, non-int value) → no gate,
      with a warning; never raises. An out-of-range int is clamped to
      ``[0, 100]`` so a direct-DB edit cannot crash the capacity gates.

    The absent-vs-null conflation is fixed: an absent key resolves to the
    Docker-tier default while an explicit ``null`` disables the gate. The
    Docker-tier PROVIDER FILTER and the rollout flag that activates default
    enforcement are D8's — until then the gates enforce
    :attr:`SandboxConcurrencyLimit.enforced_cap` only.
    """
    org = await get_organisation(session, org_id)
    if org is None:
        _log.warning("sandbox_concurrency.org_not_found", extra={"org_id": str(org_id)})
        return SandboxConcurrencyLimit(cap=None)
    return _resolve_sandbox_concurrency_limit(org.settings_json, org_id)


async def get_org_run_concurrency_limit(session: AsyncSession, org_id: uuid.UUID) -> int | None:
    """Read the org's run concurrency limit from ``settings_json``.

    ``None`` means no cap. Fail-open: a malformed value (non-dict settings,
    string, float, bool) or a missing org returns ``None`` with a warning and
    never raises. An out-of-range ``int`` is clamped to ``[1, 100]`` so a
    direct-DB edit cannot crash the dispatch-time admission gate.
    """
    return await _read_org_int_limit(
        session,
        org_id,
        _RUN_CONCURRENCY_KEY,
        _RUN_CONCURRENCY_MIN,
        _RUN_CONCURRENCY_MAX,
        "run_concurrency",
    )


async def get_run_api_key_ttl_seconds(session_factory: Any, org_id: uuid.UUID, node_timeout_seconds: int) -> int:
    """Per-run API-key TTL for script-mode sandboxes (FAR-296 Phase 3b).

    TTL = max(RUN_API_KEY_DEFAULT_TTL_SECONDS, node_timeout_seconds + 5 min),
    capped by the org-level ``run_api_key_max_ttl_seconds`` setting (default
    3600 = 1 hour). The floor comes from the ``run_api_key_default_ttl_seconds``
    setting (default 900 = 15 min) so operators can tune the leaked-key
    exposure window without redeploying. Read via the existing org
    settings_json resolution pattern (``_read_org_int_limit``) on a fresh
    session. Fail-open: a settings/org read failure falls back to the defaults.
    """
    from modulo.db.rls import set_rls_org
    from modulo.settings import get_settings

    settings_floor = 900
    org_max = 3600
    try:
        settings_floor = int(get_settings().run_api_key_default_ttl_seconds)
        async with session_factory() as session, session.begin():
            await set_rls_org(session, org_id)
            raw = await _read_org_int_limit(
                session,
                org_id,
                "run_api_key_max_ttl_seconds",
                300,
                86400,
                "run_api_key_ttl",
            )
            if raw is not None:
                org_max = raw
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("run_api_key_ttl.read_failed", extra={"org_id": str(org_id)})
    return min(max(settings_floor, node_timeout_seconds + 300), org_max)


def _percentile(sorted_data: list[float], p: float) -> float:
    """Linear interpolation percentile."""
    if not sorted_data:
        return 0.0
    k = (len(sorted_data) - 1) * p / 100
    f = int(k)
    c = min(f + 1, len(sorted_data) - 1)
    return sorted_data[f] + (k - f) * (sorted_data[c] - sorted_data[f])


def _empty_run_stats() -> dict[str, Any]:
    """Zero-shaped stats response for an empty window (same shape as a full one)."""
    return {
        "total_runs": 0,
        "success_rate": 0.0,
        "avg_duration_ms": 0,
        "p50_duration_ms": 0,
        "p95_duration_ms": 0,
        "p99_duration_ms": 0,
        "runs_by_day": [],
        "failure_by_reason": [],
        "avg_duration_by_day": [],
    }


async def _get_dialect_name(session: AsyncSession) -> str:
    """Return the active SQLAlchemy dialect name (e.g. 'postgresql')."""
    bind = session.get_bind()
    if asyncio.iscoroutine(bind):
        bind = await bind
    return bind.dialect.name


# Public alias (qa F15): core modules that need a dialect guard (the HITL gate
# coalescing's Postgres-only advisory lock / server-side JSON key filter) read
# the same helper instead of reimplementing the bind dance.
get_dialect_name = _get_dialect_name


async def get_run_stats(
    session: AsyncSession,
    period: str = "30d",
) -> dict[str, Any]:
    """Aggregated run stats for the given period (7d|30d|90d).

    Postgres computes the p50/p95/p99 duration percentiles in SQL via
    ``percentile_cont`` so the endpoint does not load every run in the window
    into Python. Generic backends (SQLite, MariaDB) fall back to loading runs
    and computing percentiles in Python because ``percentile_cont`` is
    Postgres-only. The response shape is identical on both paths.
    """
    days = {"7d": 7, "30d": 30, "90d": 90}.get(period, 30)
    cutoff = datetime.now(UTC) - timedelta(days=days)

    dialect = await _get_dialect_name(session)
    if dialect == "postgresql":
        return await _get_run_stats_postgres(session, cutoff)
    return await _get_run_stats_python(session, cutoff)


def _completed_durations_ms(completed_runs: list[Run]) -> list[int]:
    """Wall-clock durations (ms) of completed runs, sorted ascending."""
    return sorted(
        int((r.completed_at - r.started_at).total_seconds() * 1000)
        for r in completed_runs
        if r.completed_at is not None and r.started_at is not None
    )


def _runs_by_day(runs: list[Run]) -> dict[str, dict[str, int]]:
    """Per-day total / success / failed run counts."""
    by_day: dict[str, dict[str, int]] = defaultdict(lambda: {"count": 0, "success": 0, "failed": 0})
    for r in runs:
        day = r.created_at.strftime(_DAY_FORMAT)
        by_day[day]["count"] += 1
        if r.status == "complete":
            by_day[day]["success"] += 1
        elif _is_failure_bucket_status(r.status):
            by_day[day]["failed"] += 1
    return by_day


def _duration_by_day(completed_runs: list[Run]) -> dict[str, list[int]]:
    """Per-day duration samples (ms) of completed runs."""
    dur_by_day: dict[str, list[int]] = defaultdict(list)
    for r in completed_runs:
        day = r.created_at.strftime(_DAY_FORMAT)
        if r.completed_at is None or r.started_at is None:
            continue
        ms = int((r.completed_at - r.started_at).total_seconds() * 1000)
        dur_by_day[day].append(ms)
    return dur_by_day


def _failure_reason_counts(runs: list[Run]) -> dict[str, int]:
    """Counts of failure-reason statuses by their ``error_code``."""
    failure_reasons: dict[str, int] = defaultdict(int)
    for r in runs:
        if _is_failure_reason_status(r.status) and r.error_code:
            failure_reasons[r.error_code] += 1
    return failure_reasons


async def _get_run_stats_python(
    session: AsyncSession,
    cutoff: datetime,
) -> dict[str, Any]:
    """Generic-backend fallback: load runs into Python, compute percentiles locally."""
    result = await session.execute(
        select(Run)
        .join(Pipeline, Run.pipeline_id == Pipeline.id)
        .where(
            Run.created_at >= cutoff,
            Pipeline.deleted_at.is_(None),
        )
        .order_by(Run.created_at)
    )
    runs: list[Run] = list(result.scalars().all())

    total = len(runs)
    if total == 0:
        return _empty_run_stats()

    completed_runs = [r for r in runs if r.completed_at and r.started_at]
    durations_ms = _completed_durations_ms(completed_runs)

    success_count = sum(1 for r in runs if r.status == "complete")
    success_rate = round(success_count / total, 4)
    avg_duration = int(sum(durations_ms) / len(durations_ms)) if durations_ms else 0

    by_day = _runs_by_day(runs)
    dur_by_day = _duration_by_day(completed_runs)
    failure_reasons = _failure_reason_counts(runs)

    return {
        "total_runs": total,
        "success_rate": success_rate,
        "avg_duration_ms": avg_duration,
        "p50_duration_ms": int(_percentile([float(x) for x in durations_ms], 50)),
        "p95_duration_ms": int(_percentile([float(x) for x in durations_ms], 95)),
        "p99_duration_ms": int(_percentile([float(x) for x in durations_ms], 99)),
        "runs_by_day": [{"date": d, **v} for d, v in sorted(by_day.items())],
        "failure_by_reason": [
            {"reason": r, "count": c} for r, c in sorted(failure_reasons.items(), key=lambda x: -x[1])
        ],
        "avg_duration_by_day": [{"date": d, "avg_ms": int(sum(v) / len(v))} for d, v in sorted(dur_by_day.items())],
    }


async def _get_run_stats_postgres(
    session: AsyncSession,
    cutoff: datetime,
) -> dict[str, Any]:
    """Postgres fast path: duration percentiles computed in SQL via ``percentile_cont``.

    RLS scoping still applies — the queries are ORM selects against ``Run`` and
    ``Pipeline`` and the route sets the org context with ``set_rls_org`` before
    calling this function. NULL durations are excluded from the percentile
    aggregates (a run without both ``started_at`` and ``completed_at`` has no
    duration); an empty percentile group yields ``None`` in the response.
    """
    duration_ms = func.extract("epoch", Run.completed_at - Run.started_at) * 1000
    base_where = (
        Run.created_at >= cutoff,
        Pipeline.deleted_at.is_(None),
    )
    day = cast(Run.created_at, Date).label("day")

    # Per-day count/success/failed buckets plus per-day average duration. A day
    # with runs but no completed durations has a NULL avg and is omitted, which
    # matches the generic path (days only appear once they have a duration).
    day_rows = list(
        (
            await session.execute(
                select(
                    day,
                    func.count().label("run_count"),
                    func.sum(case((Run.status == "complete", 1), else_=0)).label("success"),
                    func.sum(case((Run.status.in_(_FAILURE_BUCKET_STATUSES), 1), else_=0)).label("failed"),
                    func.avg(duration_ms).label("avg_duration"),
                )
                .select_from(Run)
                .join(Pipeline, Run.pipeline_id == Pipeline.id)
                .where(*base_where)
                .group_by(day)
            )
        ).all()
    )

    total = sum(int(row.run_count) for row in day_rows)
    if total == 0:
        return _empty_run_stats()

    # Whole-window duration percentiles + mean over completed runs (both
    # started_at and completed_at present). percentile_cont ignores NULLs and
    # returns NULL for an empty group, so the response null-guards below.
    overall = (
        await session.execute(
            select(
                func.percentile_cont(0.5).within_group(duration_ms).label("p50"),
                func.percentile_cont(0.95).within_group(duration_ms).label("p95"),
                func.percentile_cont(0.99).within_group(duration_ms).label("p99"),
                func.avg(duration_ms).label("avg_duration"),
            )
            .select_from(Run)
            .join(Pipeline, Run.pipeline_id == Pipeline.id)
            .where(
                *base_where,
                Run.completed_at.is_not(None),
                Run.started_at.is_not(None),
            )
        )
    ).one()
    p50 = overall.p50
    p95 = overall.p95
    p99 = overall.p99
    avg_duration = overall.avg_duration

    # Failure reason breakdown for failed / eval_failed runs carrying an error code.
    failure_rows = list(
        (
            await session.execute(
                select(Run.error_code, func.count())
                .select_from(Run)
                .join(Pipeline, Run.pipeline_id == Pipeline.id)
                .where(
                    *base_where,
                    Run.status.in_(_FAILURE_REASON_STATUSES),
                    Run.error_code.is_not(None),
                    Run.error_code != "",
                )
                .group_by(Run.error_code)
            )
        ).all()
    )

    success_count = sum(int(row.success) for row in day_rows)
    success_rate = round(success_count / total, 4)

    return {
        "total_runs": total,
        "success_rate": success_rate,
        "avg_duration_ms": int(avg_duration) if avg_duration is not None else 0,
        "p50_duration_ms": int(p50) if p50 is not None else None,
        "p95_duration_ms": int(p95) if p95 is not None else None,
        "p99_duration_ms": int(p99) if p99 is not None else None,
        "runs_by_day": [
            {"date": str(row.day), "count": int(row.run_count), "success": int(row.success), "failed": int(row.failed)}
            for row in sorted(day_rows, key=attrgetter("day"))
        ],
        "failure_by_reason": [
            {"reason": reason, "count": int(count)}
            for reason, count in sorted(failure_rows, key=lambda item: -int(item[1]))
        ],
        "avg_duration_by_day": [
            {"date": str(row.day), "avg_ms": int(row.avg_duration)}
            for row in sorted(day_rows, key=attrgetter("day"))
            if row.avg_duration is not None
        ],
    }


async def get_run_heatmap(
    session: AsyncSession,
    year: int,
) -> list[dict[str, Any]]:
    """Run counts per day for the given year (for calendar heatmap)."""
    cutoff_start = datetime(year, 1, 1, tzinfo=UTC)
    cutoff_end = datetime(year + 1, 1, 1, tzinfo=UTC)

    result = await session.execute(
        select(Run)
        .join(Pipeline, Run.pipeline_id == Pipeline.id)
        .where(
            Run.created_at >= cutoff_start,
            Run.created_at < cutoff_end,
            Pipeline.deleted_at.is_(None),
        )
        .order_by(Run.created_at)
    )
    runs: list[Run] = list(result.scalars().all())

    by_day: dict[str, int] = defaultdict(int)
    for r in runs:
        by_day[r.created_at.strftime(_DAY_FORMAT)] += 1

    return [{"date": d, "count": c} for d, c in sorted(by_day.items())]


async def batch_delete_old_terminal_runs(
    session: AsyncSession,
    *,
    max_age_days: int = 90,
    batch_size: int = 500,
) -> int:
    """Delete terminal runs older than *max_age_days* in batches.

    Only affects runs with a terminal status (``TERMINAL_STATUSES``).
    Returns total deleted count.
    """
    cutoff = datetime.now(UTC) - timedelta(days=max_age_days)
    deleted_total = 0
    while True:
        ids = list(
            (
                await session.execute(
                    select(Run.id)
                    .where(
                        Run.status.in_(TERMINAL_STATUSES),
                        Run.created_at < cutoff,
                    )
                    .limit(batch_size)
                )
            )
            .scalars()
            .all()
        )
        if not ids:
            break
        await session.execute(delete(Run).where(Run.id.in_(ids)))
        deleted_total += len(ids)
        if len(ids) < batch_size:
            break
    return deleted_total


async def purge_runs(
    session: AsyncSession,
    *,
    older_than: str,
    batch_size: int = 500,
) -> dict[str, int]:
    """Delete terminal runs completed before *older_than* date, in batches.

    Requires RLS org context to be set by the caller.
    Returns dict with ``deleted_run_count``.
    """
    try:
        cutoff = datetime.strptime(older_than, _DAY_FORMAT).replace(tzinfo=UTC)
    except ValueError as exc:
        raise ValueError(f"Invalid date format: '{older_than}'. Expected YYYY-MM-DD.") from exc
    deleted_total = 0
    while True:
        ids = list(
            (
                await session.execute(
                    select(Run.id)
                    .where(
                        Run.status.in_(TERMINAL_STATUSES),
                        Run.completed_at < cutoff,
                    )
                    .limit(batch_size)
                )
            )
            .scalars()
            .all()
        )
        if not ids:
            break
        await session.execute(delete(Run).where(Run.id.in_(ids)))
        deleted_total += len(ids)
        if len(ids) < batch_size:
            break
    return {"deleted_run_count": deleted_total}


async def cancel_run(
    session: AsyncSession,
    run_id: uuid.UUID,
    error_code: str = "cancelled",
) -> uuid.UUID | None:
    """Atomically cancel a run that is still in pending/running status."""
    result = await session.execute(
        text("""
            UPDATE runs
            SET status = 'failed',
                error_code = :error_code,
                completed_at = NOW()
            WHERE id = :run_id
              AND status IN ('running', 'pending')
            RETURNING id
        """),
        {"error_code": error_code, "run_id": run_id},
    )
    row = result.fetchone()
    if row:
        _log.warning("CRUD cancelled run %s with error_code=%s", run_id, error_code)
        return uuid.UUID(str(row[0]))
    return None
