"""Feedback status transitions and constants."""

from collections.abc import Callable
from typing import Any

_VALID_FEEDBACK_HANDLER_TYPES = frozenset(
    {
        "human",
        "ai_correction",
        "ai_correction_with_human_review",
    }
)
_AI_HANDLER_TYPES = frozenset(
    {
        "ai_correction",
        "ai_correction_with_human_review",
    }
)
_POST_CORRECTION_EVAL_NAME = "post_correction_eval"
_DEFAULT_PAGE_SIZE = 20
_MAX_PAGE_SIZE = 100

VALID_STATUS_TRANSITIONS: dict[str, set[str]] = {
    "pending": {"routing", "correcting", "resolved", "dismissed"},
    "routing": {"escalated", "correcting", "resolved", "dismissed"},
    "correcting": {"correcting", "resolved", "escalated", "dismissed"},
    "escalated": {"resolved", "dismissed"},
    "resolved": set(),
    "dismissed": set(),
}

# Statuses on which a single-node correction outcome may NEVER be written: the
# human has already decided (``escalated`` -> HITL review, ``resolved``,
# ``dismissed``). Re-entering one via the correction path would silently reverse
# that decision, so dispatch/resume gates on non-terminal status and
# ``_persist_correction_outcome`` fences its writes on the ``correcting``
# pre-state (review FAR-210 finding 3).
CORRECTION_TERMINAL_STATUSES = frozenset({"resolved", "escalated", "dismissed"})


def handler_type_label(handler_type: str) -> str:
    """Return a safe, non-tainted label for ``feedback_handler_type``.

    The raw value arrives from caller input and is treated as tainted by
    static analysis. Each branch returns a literal constant (never the
    caller-supplied value), so the result is untainted and safe to log.
    """
    if handler_type == "human":
        return "human"
    if handler_type == "ai_correction":
        return "ai_correction"
    if handler_type == "ai_correction_with_human_review":
        return "ai_correction_with_human_review"
    return "unknown"


def prior_states_for_retry(prior_states: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return prior states with ``input_fingerprint`` stripped for retry attempts.

    Within the single-node correction's retry loop the corrected INPUT is
    unchanged across attempts, so a prior state's own ``input_fingerprint``
    would match on every retry and spuriously converge the correction before
    the fresh LM attempt runs. Output fingerprints are preserved so a repeated
    produced output (genuine oscillation) still converges. The INPUT violation
    metric is likewise stripped (the same input carries the same metric on
    every retry — a repeated input metric is not oscillation); the OUTPUT
    violation metric is preserved so a strictly-worse OR repeated output
    violation still converges.
    """
    stripped: list[dict[str, Any]] = []
    for state in prior_states:
        entry = dict(state)
        entry.pop("input_fingerprint", None)
        entry.pop("input_violation_metric", None)
        stripped.append(entry)
    return stripped


def guardrail_correction_config(guardrail: Any) -> dict[str, Any] | None:
    """Return a guardrail's embedded ``correction`` config block, or None."""
    config = getattr(guardrail, "config", None)
    if not isinstance(config, dict):
        return None
    correction = config.get("correction")
    return correction if isinstance(correction, dict) else None


def correction_guardrail_from(
    guardrails: list[Any],
) -> tuple[Any | None, dict[str, Any] | None]:
    """Return the first (guardrail, correction_config) declaring an embedded correction block."""
    for guardrail in guardrails:
        block = guardrail_correction_config(guardrail)
        if block is not None:
            return guardrail, block
    return None, None


def rls(method: Callable[..., Any]) -> Callable[..., Any]:
    """Decorator for RLS-scoped methods (no-op wrapper for now)."""
    import functools

    @functools.wraps(method)
    async def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        return await method(self, *args, **kwargs)

    return wrapper
