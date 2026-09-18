"""HITL gate coalescing (FAR-604 D4) — no duplicate open gate per work item.

The webhook queue coalescing (PR #44) folds repeat deliveries onto a PENDING
run, but a delivery that lands while the previous run is already parked at a
HITL gate mints a SECOND run that executes up to the same gate — the human
then sees multiple open gates for one work item (Housekeeper re-dispatches
every 15 minutes, so the pile grows fast).

:func:`evaluate_gate_coalescing` runs at gate-raise time (executor
``_handle_graph_interrupt``) and decides between:

* **reuse** — an OPEN gate (undecided + unclaimed) already exists on ANOTHER
  LIVE run (``awaiting_human``/``hitl_parked``) of the same pipeline for the
  same work item (same ``_coalesce_key`` stamp, same gate id) and the entity
  SHA (``runs.input_hash``) is UNCHANGED:
  the existing gate decides for the work item. The duplicate run does NOT
  raise a gate; the executor terminalises it ``failed``/``executor_superseded``
  (the house supersession code, ``run.superseded`` spec — silent severity, no
  alert).
* **supersede** — an open gate exists but the SHA CHANGED (the work item
  moved on; the old gate reviews obsolete state): the old gate is auto-closed
  with a SYSTEM-committed ``rejected`` decision (loudly audited as
  ``hitl.gate_superseded``) and the old run — if it was parked — is un-parked
  to ``awaiting_human`` so the existing committed-decision resume machinery
  (dispatcher_reconcile F6a) terminalises it through the normal reject path.
  The new run raises its gate fresh.
* **raise** — no open gate for the key (or the run carries no coalesce key,
  e.g. non-webhook triggers): normal gate creation.

Concurrency + isolation (qa pass F2/F3/F9/F12): the whole scan-and-close
sequence is serialised per work item by a transaction-scoped Postgres
advisory lock (so two concurrent duplicate deliveries cannot both
scan-then-supersede), the candidate scan only considers gates of LIVE runs
(never a terminal run's orphaned gate), and on Postgres the work-item key
match is pushed server-side (``jsonb_extract_path_text``) so the bounded
scan stops hauling candidates' full JSONB payloads.

The model does NOT support multiple runs per gate (``uq_hitl_claims_run_gate``
keys gate rows per run), so the "link the new run to the existing gate" shape
is unavailable — reuse = skip the new gate and let the existing one decide.

CLAIMED gates are never superseded (a human holding the claim is mid-review;
the claim TTL + a later raise close the loop). PARK ≠ DECIDE is preserved in
the park sweep; the supersede close-out here is an explicit D4 system decision
on obsolete work, not an expiry action.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.audit_logger import append_audit_event
from modulo.db.crud.run import (
    COALESCE_CANDIDATE_LIMIT,
    COALESCE_KEY_FIELD,
    get_dialect_name,
    read_coalesce_key,
    unpark_parked_run,
)
from modulo.db.models.hitl_claim import HitlClaim
from modulo.db.models.run import AWAITING_HUMAN_STATUS, HITL_PARKED_STATUS, Run

_log = logging.getLogger(__name__)

# Bounded candidate scan: open gates of ONE pipeline are few (an open gate is
# an awaiting human); the cap is the SHARED queue-coalescing bounded-scan
# constant (qa F15 — one owner for the convention).
_GATE_COALESCE_CANDIDATE_LIMIT = COALESCE_CANDIDATE_LIMIT

CoalesceOutcome = Literal["raise", "reuse"]

_SUPERSEDE_REASON = "superseded_by_newer_payload (FAR-604 D4 gate coalescing)"

# Statuses whose open gates are legitimate reuse candidates (qa F3): a
# terminal run's open gate is orphaned — the run will never resume to act on
# a decision made through it — so it must never decide for a fresh delivery.
_LIVE_RUN_STATUSES = (AWAITING_HUMAN_STATUS, HITL_PARKED_STATUS)


def _coalesce_lock_key(org_id: uuid.UUID, pipeline_id: uuid.UUID, gate_id: str, entity_key: str) -> str:
    """The advisory-lock key naming one work item's coalescing decision."""
    return f"{org_id}:{pipeline_id}:{gate_id}:{entity_key}"


async def _serialise_coalesce_scan(session: AsyncSession, lock_key: str) -> None:
    """Take the per-work-item coalescing advisory lock (qa F2).

    ``pg_advisory_xact_lock`` is transaction-scoped (released at COMMIT/
    ROLLBACK — never leaked by an early return or an exception) and the
    key is hashed by ``hashtext`` inside the engine. Two concurrent duplicate
    deliveries for the same work item serialise here: the second cannot run
    its candidate scan while the first is between the scan and the supersede
    close-out, so the "both saw an open gate, both closed it" / "reuse issued
    against a gate being closed" interleavings are impossible.

    Postgres-only (advisory locks are a Postgres feature); other backends
    rely on their own serialisation (SQLite is single-writer).
    """
    if await get_dialect_name(session) != "postgresql":
        return
    await session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": lock_key})


async def evaluate_gate_coalescing(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    gate_id: str,
    pipeline_id: uuid.UUID,
    org_id: uuid.UUID,
) -> CoalesceOutcome:
    """Decide raise-vs-reuse-vs-supersede for a run reaching its HITL gate.

    Runs INSIDE the caller's transaction (the executor's gate-raise session,
    RLS org context set by the caller). Side effects only ever happen on the
    ``supersede`` path (old gate closed + old run un-parked); ``reuse`` and
    ``raise`` are pure reads plus audit events. Failures of the audit append
    are fail-open (an audit loss must never block the gate raise).

    Returns ``"reuse"`` when the caller must NOT create a gate for *run_id*
    (the existing open gate decides; the executor terminalises the duplicate
    run), ``"raise"`` when the caller proceeds with normal gate creation.
    """
    run_row = (
        await session.execute(
            select(Run.input_payload, Run.input_hash).where(Run.id == run_id, Run.organisation_id == org_id)
        )
    ).first()
    if run_row is None:
        return "raise"
    run_payload, run_hash = run_row[0], run_row[1]
    entity_key = read_coalesce_key(run_payload)
    if not entity_key:
        # Non-webhook (or replay) runs carry no coalesce key — never coalesced.
        return "raise"

    # qa F2: concurrent duplicate deliveries for this work item serialise
    # before any of them scans (the lock lives to transaction end).
    await _serialise_coalesce_scan(session, _coalesce_lock_key(org_id, pipeline_id, gate_id, entity_key))

    dialect_postgres = await get_dialect_name(session) == "postgresql"
    columns: list[Any] = [HitlClaim, Run.input_hash]
    if not dialect_postgres:
        # Non-Postgres backends have no server-side JSON path filter: the
        # key match happens client-side over the bounded scan, so the
        # candidates' payloads must be hauled (and compared) here.
        columns.append(Run.input_payload)
    stmt = (
        select(*columns)
        .join(Run, Run.id == HitlClaim.run_id)
        .where(
            HitlClaim.pipeline_id == pipeline_id,
            HitlClaim.organisation_id == org_id,
            HitlClaim.gate_id == gate_id,
            HitlClaim.decision.is_(None),
            HitlClaim.account_id.is_(None),
            HitlClaim.run_id != run_id,
            Run.cancellation_requested.is_(False),
            # qa F3: only gates of LIVE runs are reuse candidates — a
            # terminal run's open gate is orphaned and decides for nobody.
            Run.status.in_(_LIVE_RUN_STATUSES),
        )
        .order_by(HitlClaim.created_at.asc())
        .limit(_GATE_COALESCE_CANDIDATE_LIMIT)
    )
    if dialect_postgres:
        # qa F9: push the work-item key filter server-side so the bounded
        # scan never loads candidates' full input_payload JSONB.
        stmt = stmt.where(func.jsonb_extract_path_text(Run.input_payload, COALESCE_KEY_FIELD) == entity_key)
    candidate_rows = (await session.execute(stmt)).all()
    open_gate = next(
        ((row[0], row[1]) for row in candidate_rows if dialect_postgres or read_coalesce_key(row[2]) == entity_key),
        None,
    )
    if open_gate is None:
        return "raise"
    old_claim, old_hash = open_gate

    if old_hash == run_hash:
        # Unchanged SHA — the existing gate decides for the work item; the
        # duplicate run is skipped (terminalised superseded by the caller).
        _log.warning(
            "hitl_coalesce.reused run=%s gate=%s existing_claim=%s existing_run=%s key=%s",
            run_id,
            gate_id,
            old_claim.id,
            old_claim.run_id,
            entity_key,
        )
        await _audit(
            session,
            org_id,
            "hitl.gate_coalesced",
            old_claim.id,
            {
                "pipeline_run_id": str(run_id),
                "node_id": gate_id,
                "reused_claim_id": str(old_claim.id),
                "reused_run_id": str(old_claim.run_id),
                "coalesce_key": entity_key,
            },
        )
        return "reuse"

    # Changed SHA — supersede: close the old gate with a SYSTEM-committed
    # rejection (guarded re-validation against a concurrent claimer) and
    # un-park the old run so the committed-decision resume machinery
    # terminalises it through the normal reject path. Then raise fresh.
    now = datetime.now(UTC)
    superseded = await session.execute(
        update(HitlClaim)
        .where(
            HitlClaim.id == old_claim.id,
            HitlClaim.organisation_id == org_id,
            HitlClaim.decision.is_(None),
            HitlClaim.account_id.is_(None),
        )
        .values(
            decision="rejected",
            decision_at=now,
            decision_payload={"action": "rejected", "gate_id": gate_id, "reason": _SUPERSEDE_REASON},
            account_id=None,
            claim_token=None,
            expires_at=now,
        )
    )
    if superseded.rowcount:  # type: ignore[attr-defined]  # ORM update Result exposes rowcount at runtime
        # Shared un-park transition (qa F15) — same helper the decision path
        # (HITLManager._decide) uses, guarded to parked runs only.
        await unpark_parked_run(session, run_id=old_claim.run_id, org_id=org_id)
        _log.warning(
            "hitl_coalesce.superseded old_claim=%s old_run=%s new_run=%s gate=%s key=%s",
            old_claim.id,
            old_claim.run_id,
            run_id,
            gate_id,
            entity_key,
        )
        await _audit(
            session,
            org_id,
            "hitl.gate_superseded",
            old_claim.id,
            {
                "pipeline_run_id": str(old_claim.run_id),
                "node_id": gate_id,
                "superseded_by_run_id": str(run_id),
                "coalesce_key": entity_key,
                "reason": _SUPERSEDE_REASON,
            },
        )
    else:
        # A claimer took the old gate between the scan and the close-out —
        # leave it to the human + claim TTL; raise the new gate fresh.
        _log.info(
            "hitl_coalesce.supersede_skipped_claimed old_claim=%s new_run=%s gate=%s",
            old_claim.id,
            run_id,
            gate_id,
        )
    return "raise"


async def _audit(
    session: AsyncSession,
    org_id: uuid.UUID,
    event_type: str,
    claim_id: uuid.UUID,
    payload: dict[str, Any],
) -> None:
    """Best-effort audit append (fail-open — never blocks the gate raise)."""
    try:
        await append_audit_event(
            session,
            org_id=org_id,
            event_type=event_type,
            resource_type="hitl_claim",
            resource_id=claim_id,
            payload_json=payload,
        )
    except Exception:
        _log.warning("hitl_coalesce.audit_failed event=%s claim=%s", event_type, claim_id)
