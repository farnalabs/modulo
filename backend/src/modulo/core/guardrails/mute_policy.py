"""Guardrail mute policy evaluation (FAR-2028).

Re-exports and wraps the eval mute policy for guardrail-specific usage.
Guardrails are eval definitions with eval_type='guardrail', so they share
the same mute policy logic.
"""

from modulo.core.eval.mute_policy import (
    DEFAULT_MUTE_WINDOW_DAYS,
    evaluate_mute_policy_for_definition,
    get_definitions_near_mute_threshold,
    get_muted_definitions,
    record_mute_audit_event,
)

__all__ = [
    "DEFAULT_MUTE_WINDOW_DAYS",
    "evaluate_mute_policy_for_definition",
    "get_definitions_near_mute_threshold",
    "get_muted_definitions",
    "record_mute_audit_event",
]
