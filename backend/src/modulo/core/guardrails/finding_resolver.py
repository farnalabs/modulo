"""Guardrail finding resolution logic (FAR-2028).

Re-exports and wraps the eval finding resolver for guardrail-specific usage.
Guardrails are eval definitions with eval_type='guardrail', so they share
the same resolution and mute policy logic.
"""

from modulo.core.eval.finding_resolver import (
    RESOLUTION_VALUES,
    FindingResolutionError,
    apply_mute_policy,
    compute_acceptance_rate,
    evaluate_mute_policy,
    resolve_finding,
    unmute_definition,
)

__all__ = [
    "RESOLUTION_VALUES",
    "FindingResolutionError",
    "apply_mute_policy",
    "compute_acceptance_rate",
    "evaluate_mute_policy",
    "resolve_finding",
    "unmute_definition",
]
