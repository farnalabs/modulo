"""HITL gate coalescing (FAR-604 D4) — no duplicate open gate per work item.

The webhook queue coalescing (PR #44) folds repeat deliveries onto a PENDING
run, but a delivery that lands while the previous run is already parked at a
HITL gate mints a SECOND run that executes up to the same gate — the human
then sees multiple open gates for one work item (Housekeeper re-dispatches
every 15 minutes, so the pile grows fast).

:func:`evaluate_gate_coalescing` runs at gate-raise time (executor
``_handle_graph_interrupt``) and decides between:

* **reuse** — an OPEN gate (undecided + unclaimed) already exists on ANOTHER
  run of the same pipeline for the same work item (same ``_coalesce_key``
  stamp, same gate id) and the entity SHA (``runs.input_hash``) is UNCHANGED:
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

The model does NOT support multiple runs per gate (``uq_hitl_claims_run_gate``
keys gate rows per run), so the "link the new run to the existing gate" shape
is unavailable — reuse = skip the new gate and let the existing one decide.

CLAIMED gates are never superseded (a human holding the claim is mid-review;
the claim TTL + a later raise close the loop). PARK ≠ DECIDE is preserved in
the park sweep; the supersede close-out here is an explicit D4 system decision
on obsolete work, not an expiry action.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.audit_logger import append_audit_event
from modulo.db.crud.run import COALESCE_KEY_FIELD
from modulo.db.models.hitl_claim import HitlClaim
from modulo.db.models.run import Run

_log = logging.getLogger(__name__)

# Bounded candidate scan: open gates of ONE pipeline are few (an open gate is
# an awaiting human); the cap matches the queue-coalescing bounded-scan
# convention (db.crud.run._COALESCE_CANDIDATE_LIMIT).
_GATE_COALESCE_CANDIDATE_LIMIT = 200

CoalesceOutcome = Literal["raise", "reuse"]

_SUPERSEDE_REASON = "superseded_by_newer_payload (FAR-604 D4 gate coalescing)"


def _entity_key(payload: Any) -> str | None:
    """The work-item coalesce key stamped on a run's stored input payload."""
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (ValueError, TypeError):
            payload = None
    if not isinstance(payload, dict):
        return None
    value = payload.get(COALESCE_KEY_FIELD)
    return str(value) if value else None


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
    entity_key = _entity_key(run_payload)
    if not entity_key:
        # Non-webhook (or replay) runs carry no coalesce key — never coalesced.
        return "raise"

    candidates = (
        await session.execute(
            select(HitlClaim, Run.input_hash, Run.input_payload)
            .join(Run, Run.id == HitlClaim.run_id)
            .where(
                HitlClaim.pipeline_id == pipeline_id,
                HitlClaim.organisation_id == org_id,
                HitlClaim.gate_id == gate_id,
                HitlClaim.decision.is_(None),
                HitlClaim.account_id.is_(None),
                HitlClaim.run_id != run_id,
                Run.cancellation_requested.is_(False),
            )
            .order_by(HitlClaim.created_at.asc())
            .limit(_GATE_COALESCE_CANDIDATE_LIMIT)
        )
    ).all()
    open_gate = next(
        (
            (claim, other_hash)
            for claim, other_hash, other_payload in candidates
            if _entity_key(other_payload) == entity_key
        ),
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
        await session.execute(
            update(Run)
            .where(
                Run.id == old_claim.run_id,
                Run.organisation_id == org_id,
                Run.status == "hitl_parked",
            )
            .values(status="awaiting_human")
        )
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
