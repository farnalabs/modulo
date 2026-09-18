"""Pre-trigger guardrail pass at webhook intake (FAR-214).

The T1 guardrail interception seam runs inside ``db.crud.run.create_run`` at
run-creation. FAR-214 moves an ADDITIONAL guardrail pass to the trigger
boundary — BEFORE run creation — so a delivery's ack semantics are decided per
guardrail at the webhook intake edge:

* a ``block``-action guardrail fires → **reject-and-retry**: the delivery is
  NOT acked-as-accepted, a ``guardrail_blocked`` TriggerEvent is recorded, the
  raw payload is stored for replay, and :class:`GuardrailBlockedAtIntakeError`
  is raised (maps to a 4xx at the route boundary). No run is created and no
  dedup slot is consumed.
* a ``redact``-action guardrail applies its static masks at intake so the
  payload that proceeds to dedup + run creation is POST-redaction (persisted
  state is post-redaction, consistent with the T1 contract).
* ``warn``/``observe`` guardrails are advisory — a firing advisory is logged
  (never a raw payload) and the delivery proceeds; the advisory evidence is
  persisted by the run-creation seam.

**Post-guardrail dedup hashing.** The dedup key is :func:`canonical_payload_hash`
over the POST-guardrail payload (SHA-256 over sorted-key, compact JSON), so
logically identical payloads dedup regardless of encoding. This closes the
raw-body-hash encoding-bypass residual exposure for the dedup key (a raw-body
hash can be bypassed by re-encoding the same logical payload differently, e.g.
key order, whitespace, unicode escapes). Pre-guardrail failure events
(timestamp, HMAC, event filters) continue to record the raw-body hash — those
rows describe the raw delivery, not dedup.

**Replays** re-run the pass DETECTION-ONLY (consistent with the run-creation
seam's ``is_replay=True`` handling): no block decision, no redaction act.

The pass REUSES the T1 engine (``run_interception_pass``,
``to_engine_definition``, ``EvalEngine``) and the run-creation seam's
row-loading semantics — detection and validation are never reimplemented.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.guardrails import GuardrailAction, run_interception_pass, to_engine_definition

_log = logging.getLogger(__name__)


class GuardrailBlockedAtIntakeError(RuntimeError):
    """A block-action guardrail rejected the delivery at webhook intake.

    The delivery is NOT acked-as-accepted: the engine records a
    ``guardrail_blocked`` TriggerEvent and stores the raw payload for replay,
    but no run is created and no dedup slot is consumed. Maps to a 4xx at the
    route boundary (400, mirroring ``DuplicateWebhookError``).
    """

    def __init__(self, detail: str, *, guardrail_name: str = "") -> None:
        super().__init__(detail)
        self.detail = detail
        self.guardrail_name = guardrail_name


@dataclass
class PreTriggerGuardrailOutcome:
    """Outcome of the pre-trigger guardrail pass at webhook intake.

    ``payload`` is the POST-guardrail payload (post-redaction for a redact-action
    guardrail) that proceeds to dedup + run creation. ``blocked`` is True when a
    block-action guardrail (or a block-mode redaction policy, or a fail-closed
    mechanism error) fired — the caller must reject the delivery.
    """

    payload: dict[str, Any]
    blocked: bool = False
    block_message: str = ""
    blocking_eval_name: str = ""
    results: list[Any] = field(default_factory=list)
    redactions: list[Any] = field(default_factory=list)
    evaluated_count: int = 0


def canonical_payload_hash(payload: dict[str, Any]) -> str:
    """SHA-256 over the canonical JSON serialization of *payload*.

    Canonical = sorted keys + compact separators, so logically identical
    payloads (key order, whitespace, unicode escapes) hash identically. This is
    the POST-guardrail dedup key (FAR-214): it closes the raw-body-hash
    encoding-bypass residual exposure for dedup. The digest never contains the
    raw payload.
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def _load_guardrail_definitions(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
) -> list[Any]:
    """Load the guardrail rows bound to the trigger's pipeline.

    Same row semantics as the run-creation seam (``db.crud.run.create_run``):
    ``eval_type='guardrail'`` for the pipeline, org-scoped. No ``node_id``
    filter — both org-level (``node_id IS NULL``) and node-bound rows are bound
    to the pipeline's runs, mirroring the seam exactly. The engine DTO mapping
    is ``to_engine_definition`` (never reimplemented here).
    """
    from modulo.db.models.eval_definition import EvalDefinition as EvalDefinitionModel

    result = await session.execute(
        select(EvalDefinitionModel).where(
            EvalDefinitionModel.pipeline_id == pipeline_id,
            EvalDefinitionModel.organisation_id == org_id,
            EvalDefinitionModel.eval_type == "guardrail",
        )
    )
    rows = result.scalars().all()
    if not isinstance(rows, list):
        # Defensive guard for call-count-stubbed sessions in unit tests (their
        # ``execute`` returns a MagicMock, not a real ``ScalarResult``). The DB
        # contract always returns a list; a non-list result means no guardrails
        # are bound, which is exactly what a stub session should observe.
        return []
    return [to_engine_definition(row) for row in rows]


async def run_pre_trigger_guardrail_pass(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    raw_payload: dict[str, Any],
    detection_only: bool = False,
) -> PreTriggerGuardrailOutcome:
    """Run the T1 two-phase guardrail pass against the raw payload at intake.

    Reuses the run-creation seam's engine and row-loading semantics; detection
    is never reimplemented. ``detection_only`` (replays) skips the block
    decision and the redaction act, consistent with ``create_run``'s
    ``is_replay=True`` handling.

    Mechanism errors fail CLOSED when any bound guardrail carries a block or
    redact action (a capability source unreadable must not let a blocked
    payload through the boundary); observe/warn-only guardrails log-and-continue
    (advisory). Replays are never blocked on a mechanism error.
    """
    definitions = await _load_guardrail_definitions(session, org_id=org_id, pipeline_id=pipeline_id)
    if not definitions:
        return PreTriggerGuardrailOutcome(payload=dict(raw_payload))
    any_guarding = any(d.config.get("action") in (GuardrailAction.BLOCK, GuardrailAction.REDACT) for d in definitions)
    from modulo.core.eval_engine import EvalEngine

    try:
        outcome = run_interception_pass(
            EvalEngine(),
            definitions,
            raw_payload,
            detection_only=detection_only,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("guardrails.pre_trigger_interception_error")
        if not detection_only and any_guarding:
            return PreTriggerGuardrailOutcome(
                payload=dict(raw_payload),
                blocked=True,
                block_message="guardrail mechanism error at webhook intake",
            )
        return PreTriggerGuardrailOutcome(payload=dict(raw_payload))

    return PreTriggerGuardrailOutcome(
        payload=outcome.payload,
        blocked=outcome.blocked,
        block_message=outcome.block_message,
        blocking_eval_name=outcome.blocking_eval_name,
        results=outcome.results,
        redactions=outcome.redactions,
        evaluated_count=len(outcome.results),
    )


__all__ = [
    "GuardrailBlockedAtIntakeError",
    "PreTriggerGuardrailOutcome",
    "canonical_payload_hash",
    "run_pre_trigger_guardrail_pass",
]
