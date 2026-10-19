"""Record a HITL review that EXPIRED unanswered (FAR-1854).

The FAR-648 terminalizer (``cron_helpers._terminalize_expired_hitl_reviews``)
cancels an ``awaiting_human`` run whose gate expired, writing
``status='cancelled'`` with ``cancel_reason='hitl_review_expired'``. Before
this module the only record of that outcome was the run-level
``run.sweep_terminalised`` event, which names the run but never the gate and
never says the review went unanswered. The gate row then keeps
``decision IS NULL`` while the run is terminal, and both
:meth:`HITLManager.list_pending` and ``GET /hitl/reviews`` fence undecided
gates to actionable run statuses — so the expired review disappears from
every pending surface with no gate-level evidence anywhere.

This module appends that evidence: one explicit ``hitl.review_expired``
event per UNADJUDICATED gate on a run the sweep cancelled for expiry. It is
deliberately NOT a verdict:

* ``outcome`` is ``expired_undecided`` and ``decision`` is ``null`` — nothing
  here may ever read as an approval or a rejection.
* Only gates that are still ``decision IS NULL`` are recorded. A gate decided
  before the sweep ran already has its own decision event and gets no expiry
  event (a decision is not an expiry, even when the run was later cancelled).
* The run is re-verified here: only a run whose ``cancel_reason`` or
  ``error_code`` is the expiry marker is considered. The sweep hands this
  helper EVERY terminalised run id (from all terminalizers), so the marker
  check is what confines it to the expiry class.

Every append goes through
:func:`modulo.core.audit_logger.background.append_background_audit_event`,
the SYSTEM-actor background-audit machinery: no fabricated user, an honest
``actor_source``, and fail-open WITH a log. This runs after the sweep's
transactions have committed — an audit failure must never undo the
cancellation (best-effort, never silent).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Sequence
from typing import Any, Final

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from modulo.core.audit_logger.background import append_background_audit_event
from modulo.db.models.hitl_claim import HitlClaim
from modulo.db.models.run import CANCEL_REASON_HITL_REVIEW_EXPIRED, Run
from modulo.db.rls import set_rls_org

_log = logging.getLogger(__name__)

#: Gate-level audit event: the review reached its deadline with no decision.
EXPIRY_EVENT_TYPE: Final[str] = "hitl.review_expired"

#: The only outcome this module records. Never a verdict — a missing decision
#: is not an approval, a rejection or a manual delivery.
EXPIRY_OUTCOME: Final[str] = "expired_undecided"

#: Background process provenance stamped by ``append_background_audit_event``.
_EXPIRY_ACTOR_SOURCE: Final[str] = "dispatcher_reconcile"

#: Dedicated log key — an append failure is loud and attributable, never silent.
EXPIRY_AUDIT_LOG_KEY: Final[str] = "cron_helpers.review_expiry_audit_failed"

_SUMMARY: Final[str] = "HITL review expired unanswered; no verdict was recorded"


def _expiry_gate_select(org_id: uuid.UUID, run_ids: Sequence[uuid.UUID]) -> Any:
    """SELECT the still-undecided gates on runs cancelled for expiry.

    ``decision IS NULL`` is the whole point: a gate decided before the sweep
    already has its own decision event and must never be re-labelled as
    expired. ``cancel_reason``/``error_code`` re-verify the run is an EXPIRY
    cancellation — the sweep passes ids from every terminalizer, and this
    predicate is what keeps non-expiry cancellations out.
    """
    return (
        select(
            HitlClaim.id,
            HitlClaim.run_id,
            HitlClaim.review_id,
            Run.cancel_reason,
        )
        .join(Run, Run.id == HitlClaim.run_id)
        .where(
            HitlClaim.organisation_id == org_id,
            HitlClaim.run_id.in_(list(run_ids)),
            HitlClaim.decision.is_(None),
            Run.organisation_id == org_id,
            or_(
                Run.cancel_reason == CANCEL_REASON_HITL_REVIEW_EXPIRED,
                Run.error_code == CANCEL_REASON_HITL_REVIEW_EXPIRED,
            ),
        )
    )


async def _select_expiry_gates(
    factory: async_sessionmaker[AsyncSession],
    *,
    org_id: uuid.UUID,
    run_ids: Sequence[uuid.UUID],
) -> list[Any]:
    """Read the undecided gates for one org in its own RLS-scoped session."""
    async with factory() as session, session.begin():
        await set_rls_org(session, org_id)
        result = await session.execute(_expiry_gate_select(org_id, run_ids))
        return list(result.all())


async def record_review_expiry_outcomes(
    factory: async_sessionmaker[AsyncSession],
    cancelled: Sequence[tuple[uuid.UUID, uuid.UUID]],
) -> int:
    """Append one ``hitl.review_expired`` event per undecided expired gate.

    Args:
        factory: Session factory for the RLS-scoped read and each audit append.
        cancelled: ``(run_id, org_id)`` pairs collected by
            ``dispatcher_reconcile`` for THIS tick. The list covers every
            terminalizer, so entries whose run was not cancelled for expiry are
            filtered out by the selection predicate.

    Returns:
        The number of events recorded. Never raises for an audit failure: this
        runs after the cancellation has committed, so a failure is logged under
        :data:`EXPIRY_AUDIT_LOG_KEY` and swallowed (fail open, with a log).
        ``CancelledError`` always propagates.
    """
    if not cancelled:
        return 0

    by_org: dict[uuid.UUID, list[uuid.UUID]] = {}
    for run_id, org_id in cancelled:
        by_org.setdefault(org_id, []).append(run_id)

    recorded = 0
    for org_id, run_ids in by_org.items():
        try:
            gates = await _select_expiry_gates(factory, org_id=org_id, run_ids=run_ids)
        except asyncio.CancelledError:
            raise
        except Exception:
            # The read failed: log loudly and keep going — the cancellation is
            # already committed and must never be undone by its own record.
            _log.warning(
                EXPIRY_AUDIT_LOG_KEY,
                extra={"org_id": str(org_id), "run_count": len(run_ids), "stage": "select"},
                exc_info=True,
            )
            continue

        for claim_id, run_id, review_id, cancel_reason in gates:
            payload: dict[str, Any] = {
                "summary": _SUMMARY,
                "pipeline_run_id": str(run_id),
                "node_id": review_id,
                "outcome": EXPIRY_OUTCOME,
                # Explicit NULL: no verdict was recorded. This key must be
                # present so a reader can tell "no decision" from "field
                # omitted" without inferring it.
                "decision": None,
                "cancel_reason": cancel_reason,
            }
            try:
                appended = await append_background_audit_event(
                    factory,
                    org_id=org_id,
                    event_type=EXPIRY_EVENT_TYPE,
                    resource_type="hitl_claim",
                    resource_id=claim_id,
                    actor_source=_EXPIRY_ACTOR_SOURCE,
                    payload_json=payload,
                    log_key=EXPIRY_AUDIT_LOG_KEY,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                # Defensive: append_background_audit_event already fails open
                # with a log. If it ever raises instead, never let the record
                # of a completed cancellation become a tick failure.
                _log.warning(
                    EXPIRY_AUDIT_LOG_KEY,
                    extra={
                        "org_id": str(org_id),
                        "run_id": str(run_id),
                        "review_id": review_id,
                        "stage": "append",
                    },
                    exc_info=True,
                )
                continue
            if appended:
                recorded += 1

    return recorded
