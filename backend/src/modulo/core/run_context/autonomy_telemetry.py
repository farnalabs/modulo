"""Autonomy telemetry — the evidence record for progressive autonomy.

Progressive autonomy is the claim that a pipeline's autonomy level
(``manual_approval`` → ``notify_on_complete`` → ``fully_autonomous``) should
*rise* for change classes that consistently ship clean and *fall* after a
defect escapes human review. That claim is only defensible if every autonomy
decision a run makes is recorded as a first-class, queryable event.

This module is the single emission point for that evidence record. The
``run.autonomy_level_applied`` audit event is emitted whenever a HITL gate is
evaluated at runtime, recording:

* the **effective autonomy level** actually applied to the run
  (``effective_autonomy_level`` resolution — run_context recommendation,
  pipeline default, or the ``manual_approval`` fallback),
* the **gate outcome** — ``skipped`` (fully_autonomous), ``auto_approved``
  (notify_on_complete), or ``fired`` (human path taken / interrupt raised),
* the gate id, run id and pipeline id, so the event can be joined to the
  ``run_daily_facts`` analytics surface.

Emission is **fail-open**: a telemetry failure must never break a pipeline
run, only log. The event is appended to the tamper-evident ``audit_events``
chain (see ``modulo.core.audit_logger``), so the evidence record is the same
cryptographic audit trail that backs every other governance action in Modulo.

The event type is also registered in
``modulo.core.product_analytics.metrics_constants.VALID_EVENT_TYPES`` so it
flows through product-analytics ingest and can be surfaced in dashboards.

Data model
----------
``audit_events`` row (event_type = ``"run.autonomy_level_applied"``)::

    {
      "event_type": "run.autonomy_level_applied",
      "actor_user_id": <resolved actor or null>,      # null for autonomous decisions
      "resource_type": "run",
      "resource_id": "<run_id>",
      "payload_json": {
        "gate_id": "<gate_id>",
        "autonomy_level": "manual_approval | notify_on_complete | fully_autonomous",
        "gate_outcome": "skipped | auto_approved | fired",
        "pipeline_id": "<pipeline_id>" | null,
        "human_only": <bool>
      }
    }

See ``docs/autonomy-study.md`` for the full measurement framework and the
metrics derived from this event stream.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable
from typing import Any

_log = logging.getLogger(__name__)

# Canonical event type emitted by this module.
AUTONOMY_LEVEL_APPLIED = "run.autonomy_level_applied"

# Gate outcomes recorded in the payload.
GATE_OUTCOME_SKIPPED = "skipped"  # fully_autonomous — gate bypassed
GATE_OUTCOME_AUTO_APPROVED = "auto_approved"  # notify_on_complete
GATE_OUTCOME_FIRED = "fired"  # human path taken / interrupt raised

VALID_GATE_OUTCOMES = frozenset({GATE_OUTCOME_SKIPPED, GATE_OUTCOME_AUTO_APPROVED, GATE_OUTCOME_FIRED})


async def emit_autonomy_telemetry(
    session_factory: Callable[..., Any] | None,
    *,
    org_id: uuid.UUID | None,
    run_id: uuid.UUID | str | None,
    gate_id: str,
    autonomy_level: str,
    gate_outcome: str,
    pipeline_id: uuid.UUID | str | None = None,
    human_only: bool = False,
) -> None:
    """Append a ``run.autonomy_level_applied`` audit event (fail-open).

    Parameters
    ----------
    session_factory:
        Async session factory (``lambda: SessionLocal()`` style). When ``None``
        the call is a no-op — callers that run without a session (e.g. some
        test harnesses) must not be penalised.
    org_id:
        Organisation the run belongs to. Required for the audit chain; when
        ``None`` the call is skipped.
    run_id, gate_id, autonomy_level, gate_outcome:
        The evidence-record fields described in the module docstring.
    pipeline_id:
        Optional pipeline id, carried in the payload for joins.
    human_only:
        Whether the gate was configured ``human_only`` (always interrupts).
    """
    if session_factory is None or org_id is None:
        return
    if gate_outcome not in VALID_GATE_OUTCOMES:
        _log.warning("autonomy_telemetry: invalid gate_outcome %r — skipping", gate_outcome)
        return
    try:
        from modulo.core.audit_logger import append_audit_event
        from modulo.db.rls import set_rls_execution_context, set_rls_org

        async with session_factory() as session, session.begin():
            # STRICT RLS guards audit_events: without the org + execution-context
            # settings established inside the transaction the INSERT is rejected
            # by the rls_org_isolation policy (app.organisation_id must equal
            # organisation_id), and without session.begin() the open transaction
            # is rolled back on close so the event never persists. This mirrors
            # the sibling mid-run helper _append_conformance_audit exactly.
            await set_rls_org(session, org_id)
            await set_rls_execution_context(session)
            await append_audit_event(
                session,
                org_id=org_id,
                event_type=AUTONOMY_LEVEL_APPLIED,
                actor_user_id=None,
                resource_type="run",
                resource_id=uuid.UUID(str(run_id)) if run_id else None,
                payload_json={
                    "gate_id": gate_id,
                    "autonomy_level": autonomy_level,
                    "gate_outcome": gate_outcome,
                    "pipeline_id": str(pipeline_id) if pipeline_id else None,
                    "human_only": bool(human_only),
                },
                request_id=None,
            )
    except asyncio.CancelledError:
        raise
    except Exception:  # pragma: no cover - fail-open telemetry
        _log.exception("autonomy_telemetry: failed to record event (ignored)")
