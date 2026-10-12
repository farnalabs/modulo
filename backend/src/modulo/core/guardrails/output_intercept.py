"""Output-side guardrail interception (FAR-209 Track B).

Input-point guardrails fire at run creation (the ingestion edge, pre-persist).
Output-point guardrails (``interception_point == "output"``) fire instead at
the executor's chain-end seam — a completed node's output envelope is
intercepted BEFORE it is recorded into the run's persisted output history
(``ctx.completed_node_outputs``), so the redacted copy is what downstream
state and the API ever see.

Semantics, mirroring the ingestion edge:

* Detection is deterministic and pure (regex | json_schema) — unchanged.
* ``block`` action raises ``GuardrailBlockedError`` (an ``EvalBlockedError``
  subclass) — the executor's existing ``except EvalBlockedError`` handler
  transitions the run to terminal ``eval_failed`` unchanged.
* ``redact`` applies the field policies (transform/drop masks-only, or the
  FAR-209 ``destroy`` HMAC mode) to a deep copy.
* Replays are detection-only — no act, no re-block (item 10).
* The guardrails kill switch downgrades the pass to observe (shadow-only).
* Mechanism errors are handled by the shared pass — fail closed for
  block/redact, log-and-continue for observe/warn.

Filtering (D5 in the Track B design): an ``interception_point`` value that is
neither ``"input"`` nor ``"output"`` is treated as UNKNOWN — the output
filter excludes it AND the input-pass exclusion keeps it (computed as
``interception_point != "output"``), so an unknown value still runs the
ingestion pass and its strict pydantic config validation fails closed there
(ValidationError -> mechanism error -> block for guarding actions). No
unknown-point guardrail is ever silently unbound from both passes.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import Any, Protocol

from modulo.core.eval_engine import EvalDefinition
from modulo.core.guardrails import run_interception_pass_async

_log = logging.getLogger(__name__)

INPUT_INTERCEPTION_POINT = "input"
OUTPUT_INTERCEPTION_POINT = "output"


class SupportsGuardrailConfig(Protocol):
    """Structural contract for anything carrying a guardrail ``config`` mapping.

    Satisfied by BOTH DTOs that reach the output-point predicate: the engine's
    pydantic ``EvalDefinition`` (ingestion-edge selection) and the executor's
    ``EvalDefDTO`` (the node-output seam). The predicate reads only ``config``,
    so the seam's single source of truth serves either without a duck-typed
    ``type: ignore`` — or, worse, a second local copy of a security predicate.
    """

    config: dict[str, Any]


def is_output_interception(eval_def: SupportsGuardrailConfig) -> bool:
    """True when *eval_def* (a guardrail-typed DTO) declares an output point.

    Strictly the literal ``"output"`` — an unknown value is NOT output (it
    stays with the input pass, where strict validation fails closed).

    This is the ONE implementation: ``pipeline_engine.executor`` lazily
    imports it inside ``_handle_chain_end_event`` (a module-level import
    cycles guardrails -> eval_engine -> executor). Never re-implement it.
    """
    point = eval_def.config.get("interception_point", INPUT_INTERCEPTION_POINT)
    return bool(point == OUTPUT_INTERCEPTION_POINT)


def filter_output_interception_definitions(
    definitions: Sequence[EvalDefinition],
) -> list[EvalDefinition]:
    """Keep only the output-point guardrail defs from *definitions*."""
    return [d for d in definitions if is_output_interception(d)]


def exclude_output_interception_definitions(
    definitions: Sequence[EvalDefinition],
) -> list[EvalDefinition]:
    """Keep only the NON-output guardrail defs (input-pass helper, D5).

    Unknown ``interception_point`` values are kept — the input pass's strict
    validation then fails closed for guarding actions instead of the guardrail
    dropping silently from both passes.
    """
    return [d for d in definitions if not is_output_interception(d)]


async def read_output_interception_flags(
    session_factory: Callable[[], Any],
    *,
    run_id: Any,
    org_id: Any,
) -> tuple[bool, bool]:
    """Read ``(is_replay, kill_switch_on)`` for a run (fail-open like the seam).

    ``is_replay`` from ``runs.is_replay``; ``kill_switch_on`` from the org's
    ``guardrails_kill_switch``. A read failure defaults to
    ``(False, False)`` — normal enforcement stays active — matching the
    ingestion seam's kill-switch read direction (fail-closed for a
    data-safety control). Never logs a raw payload.
    """
    from sqlalchemy import text
    from sqlalchemy.exc import SQLAlchemyError

    try:
        async with session_factory() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT r.is_replay, o.guardrails_kill_switch "
                        "FROM runs r JOIN organisations o ON o.id = r.organisation_id "
                        "WHERE r.id = :rid"
                    ),
                    {"rid": run_id.hex if hasattr(run_id, "hex") else run_id},
                )
            ).first()
        if row is not None:
            return bool(row[0]), bool(row[1])
    except SQLAlchemyError:
        _log.warning(
            "guardrails.output_flags_read_unavailable",
            extra={"run_id": str(run_id), "org_id": str(org_id)},
        )
    return False, False


async def run_output_interception_async(
    definitions: Sequence[Any],
    envelope: dict[str, Any],
    *,
    session_factory: Callable[[], Any],
    run_id: Any,
    org_id: Any,
) -> Any:
    """Run the output-point guardrail pass over a completed node's envelope.

    Wraps :func:`run_interception_pass_async` — the engine defaults. Payload
    budget and per-guardrail timeouts come with it verbatim. Detection-only
    (replay / kill-switch-on) means the pass never acts — it records results
    only.

    Returns the ``GuardrailInterceptionOutcome``; the caller decides the
    block semantics (raise ``GuardrailBlockedError``) and records the
    redacted envelope.
    """
    from modulo.core.eval_engine import EvalEngine
    from modulo.core.guardrails import SEAM_NODE_OUTPUT_EDGE

    flag_replay, kill_switch = await read_output_interception_flags(session_factory, run_id=run_id, org_id=org_id)
    detection_only = flag_replay or kill_switch
    if kill_switch:
        _log.warning(
            "guardrails.output_kill_switch_downgrade",
            extra={"run_id": str(run_id), "org_id": str(org_id)},
        )
    return await run_interception_pass_async(
        EvalEngine(),
        list(definitions),
        envelope,
        detection_only=detection_only,
        # The shared pass labels its mechanism-error block messages with the
        # seam it ran on — without this the node-output seam would report
        # "at ingestion edge" (FAR-209 Track B review finding).
        seam=SEAM_NODE_OUTPUT_EDGE,
    )


__all__ = [
    "INPUT_INTERCEPTION_POINT",
    "OUTPUT_INTERCEPTION_POINT",
    "SupportsGuardrailConfig",
    "exclude_output_interception_definitions",
    "filter_output_interception_definitions",
    "is_output_interception",
    "read_output_interception_flags",
    "run_output_interception_async",
]
