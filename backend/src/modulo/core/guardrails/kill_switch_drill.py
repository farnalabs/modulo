"""Auditable self-test ("drill") for the org guardrail kill-switch (FAR-1707).

The kill-switch already exists: ``organisations.guardrails_kill_switch`` is read
at run start by :func:`modulo.db.crud.run._read_guardrails_kill_switch`
(fail-closed → OFF on read error) and applied by
:func:`modulo.db.crud.run._downgrade_guardrails_to_observe` (downgrades every
bound guardrail to ``observe``). What was missing is a periodic, durable
*attestation* that the control's two load-bearing invariants actually hold, so
``kill_switch_tested`` is a recorded fact rather than an assumption.

This module is the drill — a NON-MUTATING functional canary:

1. **DOWNGRADE** — with the kill-switch logically ON, every bound guardrail
   resolves to ``observe`` and the downgrade is NOT vacuous (the pre-downgrade
   set must contain at least one ``block``/``redact``).
2. **FAIL-CLOSED READ** — a kill-switch read failure resolves to OFF
   (enforcement stays active), proven by driving the real reader with a session
   whose ``execute`` raises ``SQLAlchemyError``.

It never reads or writes any real organisation's ``guardrails_kill_switch``: the
downgrade check operates on synthetic in-memory definitions, and the read check's
session fails before any query reaches a database.

The scheduled entry point is :func:`run_kill_switch_drill`, registered as a
system cron in :func:`modulo.core.saq_worker._system_cron_jobs`. It appends
exactly one record per run to the org-independent ``system_audit_events`` ledger
(migration 0285 — free-text ``event_type``, so this is migration-free) with
``event_type="kill_switch_drill"``. A failed *invariant* is a recorded fact
(``result="fail"``), never a job failure; a failed *record write* still
propagates (fail-closed ledger).
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import TYPE_CHECKING, Any, Literal, TypedDict, cast

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.system_audit_logger import append_system_audit_event

if TYPE_CHECKING:
    from modulo.core.eval_engine import EvalDefinition

__all__ = [
    "DrillOutcome",
    "DrillResult",
    "run_kill_switch_drill",
    "verify_kill_switch_invariants",
]

_log = logging.getLogger(__name__)

#: Placeholder org id for the fail-closed read canary. The drill's stub session
#: raises before the id is ever used, so this never addresses a real org.
_DRILL_ORG_ID = uuid.UUID(int=0)

#: The system-audit event type the drill appends (free-text; no CHECK).
_KILL_SWITCH_DRILL_EVENT_TYPE = "kill_switch_drill"


class DrillOutcome(TypedDict):
    """Per-check booleans for the pure DOWNGRADE invariant."""

    downgrade_applied: bool
    downgrade_non_vacuous: bool


class DrillResult(TypedDict):
    """The drill outcome recorded in ``payload_json`` and returned to the cron."""

    result: Literal["pass", "fail"]
    checks: dict[str, bool]
    duration_ms: float


class _FailingReadSession:
    """``AsyncSession`` stand-in whose ``execute`` ALWAYS raises.

    The fail-closed-read canary: :func:`verify_kill_switch_invariants` proves the
    downgrade side against synthetic data, and this session proves the reader
    degrades to OFF (not ON) when the database read fails. Because ``execute``
    raises before any statement is sent, no real organisation row is read.
    """

    async def execute(self, *args: Any, **kwargs: Any) -> Any:
        raise SQLAlchemyError("kill_switch_drill: simulated kill-switch read failure")


def _synthetic_guardrail_defs() -> list[EvalDefinition]:
    """Build one guardrail definition per action (block/redact/warn/observe)."""
    from modulo.core.eval_engine import EvalDefinition, EvalType
    from modulo.core.guardrails import GuardrailAction

    actions = (
        GuardrailAction.BLOCK,
        GuardrailAction.REDACT,
        GuardrailAction.WARN,
        GuardrailAction.OBSERVE,
    )
    return [
        EvalDefinition(
            id=uuid.uuid4(),
            org_id=_DRILL_ORG_ID,
            name=f"kill-switch-drill-{action.value}",
            eval_type=EvalType.GUARDRAIL,
            config={"action": action.value},
        )
        for action in actions
    ]


def verify_kill_switch_invariants() -> DrillOutcome:
    """Pure, DB-free check of the kill-switch DOWNGRADE invariant.

    Builds a synthetic guardrail set carrying each action, asserts the
    pre-downgrade set contains at least one ``block``/``redact`` (the non-vacuity
    guard — otherwise an all-observe input would prove nothing), applies the real
    :func:`modulo.db.crud.run._downgrade_guardrails_to_observe`, and asserts every
    resulting definition resolves to ``observe``.
    """
    from modulo.core.guardrails import GuardrailAction
    from modulo.db.crud.run import _downgrade_guardrails_to_observe

    defs = _synthetic_guardrail_defs()

    observed_actions = {str(d.config.get("action")) for d in defs}
    non_vacuous = bool(observed_actions & {GuardrailAction.BLOCK.value, GuardrailAction.REDACT.value})

    downgraded = _downgrade_guardrails_to_observe(defs)
    # Set equality (not ``all()``) so an EMPTY downgraded set fails the check
    # rather than passing vacuously — the same anti-vacuity stance as the
    # pre-downgrade guard above.
    downgraded_actions = {str(d.config.get("action")) for d in downgraded}
    downgrade_applied = bool(downgraded_actions) and downgraded_actions == {GuardrailAction.OBSERVE.value}

    return {"downgrade_applied": downgrade_applied, "downgrade_non_vacuous": non_vacuous}


async def _check_fail_closed_read() -> bool:
    """Prove the reader resolves a read failure to OFF (enforcement active)."""
    from modulo.db.crud.run import _read_guardrails_kill_switch

    faulty_session = cast(AsyncSession, _FailingReadSession())
    return await _read_guardrails_kill_switch(faulty_session, _DRILL_ORG_ID) is False


async def _run_drill_checks(session: AsyncSession) -> DrillResult:
    """Run both invariants and append exactly one audit record on *session*."""
    start = time.perf_counter()

    outcome = verify_kill_switch_invariants()
    fail_closed_read = await _check_fail_closed_read()

    checks: dict[str, bool] = {
        "downgrade_applied": outcome["downgrade_applied"],
        "downgrade_non_vacuous": outcome["downgrade_non_vacuous"],
        "fail_closed_read": fail_closed_read,
    }
    passed = all(checks.values())
    result: Literal["pass", "fail"] = "pass" if passed else "fail"
    duration_ms = (time.perf_counter() - start) * 1000.0

    if not passed:
        # A failed invariant is a recorded fact, not a job failure — log loudly
        # and still append the evidence row below.
        _log.warning("kill_switch_drill.invariant_failed", extra={"checks": checks})

    await append_system_audit_event(
        session,
        event_type=_KILL_SWITCH_DRILL_EVENT_TYPE,
        resource_type="guardrail_kill_switch",
        payload_json={"result": result, "checks": checks, "duration_ms": duration_ms},
    )

    return {"result": result, "checks": checks, "duration_ms": duration_ms}


async def run_kill_switch_drill(session_or_ctx: AsyncSession | dict[str, Any]) -> DrillResult:
    """Run the kill-switch drill and record one evidence fact.

    Callable two ways, because it is both the DB-facing core and the SAQ cron
    entry point:

    * **With a session** (``run_kill_switch_drill(session)``) — appends the
      evidence row in the caller's transaction, exactly as
      :func:`append_system_audit_event` requires.
    * **With a SAQ context** (``run_kill_switch_drill(ctx)``, how the cron
      scheduler invokes it) — opens a cross-org system session and commits the
      single append before returning.

    Invariant failures are recorded as ``result="fail"`` and never raised; a
    failure to *write* the record propagates (fail-closed ledger).
    """
    if isinstance(session_or_ctx, dict):
        # SAQ cron path: the worker hands us a plain context dict. Open a
        # cross-org system session and own the transaction. Imported lazily to
        # avoid a module import cycle (saq_worker registers this function).
        from modulo.core.saq_worker import _make_system_session_factory

        factory = _make_system_session_factory()
        async with factory() as owned_session, owned_session.begin():
            return await _run_drill_checks(owned_session)

    return await _run_drill_checks(session_or_ctx)
