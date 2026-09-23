"""Autonomy levels for pipeline execution.

Each pipeline can be configured with a default autonomy level that controls
how HITL (human-in-the-loop) gates are handled during runs.

Levels:
    manual_approval    — Gate halts execution; human must review and approve/reject.
    notify_on_complete — Gate is auto-approved at runtime but creates a notification
                         event for observability.
    fully_autonomous   — Gate is skipped entirely; no halts, no notifications.

The autonomy recommendation can be set at pipeline level
(default_autonomy_level) and may be overridden at runtime by a
context-setter agent (via autonomy_recommendation in run_context).

Escalation ceiling (FAR-1163 S0): a recommendation may LOWER autonomy freely,
but may RAISE it only up to the pipeline's ``max_autonomy_level`` ceiling —
pinned into the run snapshot as the reserved ``_pipeline_max_autonomy``
run_context key. When no ceiling is pinned, the effective ceiling is the
pipeline's default, so a recommendation can only lower autonomy. Every clamp
is recorded by ``emit_autonomy_clamp_telemetry``.
"""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass
from typing import Any

_log = logging.getLogger(__name__)


class AutonomyLevel(enum.StrEnum):
    """Autonomy level for a pipeline run's HITL gate behaviour."""

    MANUAL_APPROVAL = "manual_approval"
    NOTIFY_ON_COMPLETE = "notify_on_complete"
    FULLY_AUTONOMOUS = "fully_autonomous"

    @classmethod
    def _missing_(cls, value: object) -> AutonomyLevel:
        if isinstance(value, str):
            for member in cls:
                if member.value == value.lower():
                    return member
        msg = f"Invalid autonomy level: {value!r}"
        raise ValueError(msg)

    @classmethod
    def default(cls) -> AutonomyLevel:
        """Return the safest autonomy level (manual_approval)."""
        return cls.MANUAL_APPROVAL


AUTONOMY_LEVEL_VALUES = [m.value for m in AutonomyLevel]

# Explicit ordering: manual_approval < notify_on_complete < fully_autonomous.
# Levels are NOT ordered lexicographically ("fully_autonomous" sorts first as a
# raw string), so every min/max/comparison in this module goes through the rank
# map — never through StrEnum string ordering.
_LEVEL_RANK: dict[AutonomyLevel, int] = {
    AutonomyLevel.MANUAL_APPROVAL: 0,
    AutonomyLevel.NOTIFY_ON_COMPLETE: 1,
    AutonomyLevel.FULLY_AUTONOMOUS: 2,
}


def autonomy_level_rank(value: AutonomyLevel) -> int:
    """Numeric rank of a level (0 = manual_approval … 2 = fully_autonomous)."""
    return _LEVEL_RANK[value]


# Reserved run_context key pinning the pipeline's max_autonomy_level ceiling
# into the run (seeded by the executor from the snapshot; see decorator
# _RESERVED_RUN_CONTEXT_KEYS).
PIPELINE_MAX_AUTONOMY_KEY = "_pipeline_max_autonomy"


@dataclass(frozen=True)
class AutonomyResolution:
    """Result of ceiling-aware autonomy resolution at a HITL gate.

    effective:
        The level actually applied (already clamped against the ceiling).
    requested:
        The raw ``autonomy_recommendation`` the context-setter asked for, or
        ``None`` when no recommendation was present.
    ceiling:
        The effective ceiling the recommendation was clamped against.
    clamped:
        True when a recommendation above the base was reduced to the ceiling
        (drives the ``run.autonomy_recommendation_clamped`` audit event).
    """

    effective: AutonomyLevel
    requested: AutonomyLevel | None
    ceiling: AutonomyLevel
    clamped: bool


def _try_autonomy(value: str | None, label: str) -> AutonomyLevel | None:
    if value is None:
        return None
    try:
        return AutonomyLevel(value)
    except ValueError:
        _log.warning("Invalid %s %r — falling back", label, value)
        return None


def _min_level(a: AutonomyLevel, b: AutonomyLevel) -> AutonomyLevel:
    """Order two levels by rank (never by StrEnum string ordering)."""
    return a if autonomy_level_rank(a) <= autonomy_level_rank(b) else b


def resolve_autonomy(
    pipeline_default: str | None,
    run_context: dict[str, Any] | None = None,
) -> AutonomyResolution:
    """Resolve the autonomy level for a run, clamped to the pipeline ceiling.

    Pseudocode (ADR 043 §2, S0 slice — ``earned_autonomy_level`` not yet in
    play, so ``base`` is derived from the pinned default)::

        ceiling = run_context[_pipeline_max_autonomy] or pipeline_default or manual_approval
        base    = min(pipeline_default or manual_approval, ceiling)
        rec     = run_context.autonomy_recommendation
        if rec is None:     effective = base
        elif rec <= base:   effective = rec            # lowering always allowed
        else:               effective = min(rec, ceiling)  # raising capped; clamp audited
        clamped = rec is not None and effective != rec

    With the default ceiling (== the pipeline default) a recommendation can
    ONLY lower autonomy; a ceiling above the default re-opens raising up to
    that ceiling. With no ceiling pinned at all, the effective ceiling is the
    default — the S0 fix for the context-setter escalation hole.
    """
    ceiling_raw: str | None = None
    rec_raw: str | None = None
    if isinstance(run_context, dict):
        rec_raw = run_context.get("autonomy_recommendation")
        ceiling_raw = run_context.get(PIPELINE_MAX_AUTONOMY_KEY)
    elif run_context is not None:
        _log.warning("run_context is not a dict (got %s), ignoring", type(run_context).__name__)

    rec = _try_autonomy(rec_raw, "run_context autonomy_recommendation")
    default_level = _try_autonomy(pipeline_default, "pipeline_default_autonomy_level")
    ceiling = (
        _try_autonomy(ceiling_raw, "run_context _pipeline_max_autonomy") or default_level or AutonomyLevel.default()
    )
    base_default = default_level or AutonomyLevel.default()
    base = _min_level(base_default, ceiling)

    if rec is None:
        effective = base
    elif autonomy_level_rank(rec) <= autonomy_level_rank(base):
        # Lowering is always allowed.
        effective = rec
    else:
        # Raising is capped at the ceiling; the clamp is audited by the caller.
        effective = _min_level(rec, ceiling)

    # ``effective != rec`` already implies rec > base (the lowering/equal
    # branch sets effective = rec), so only these two terms are needed.
    clamped = rec is not None and effective != rec
    return AutonomyResolution(effective=effective, requested=rec, ceiling=ceiling, clamped=clamped)


def effective_autonomy_level(
    pipeline_default: str | None,
    run_context: dict[str, Any] | None = None,
) -> AutonomyLevel:
    """Resolve the effective autonomy level for a run (thin wrapper).

    Backward-compatible facade over :func:`resolve_autonomy` — existing
    callers and tests depend on this signature. See ``resolve_autonomy`` for
    the ceiling-clamped priority rules.
    """
    return resolve_autonomy(pipeline_default, run_context).effective


def validate_autonomy_ceiling(
    default: str | None,
    ceiling: str | None,
    *,
    lenient: bool = False,
) -> None:
    """Raise ``ValueError`` when *ceiling* sits below *default*.

    A ``None`` ceiling imposes no violation: resolution falls back to the
    default as the effective ceiling. When *lenient* is True an unparseable /
    non-string ceiling is treated the same as ``None`` (resolution also falls
    back in that case) — used for values read back from storage. An
    unparseable *default* is ranked as ``manual_approval``, the same fallback
    resolution applies, so it can never fail this check by itself.

    A non-canonical case-variant spelling (e.g. ``"MANUAL_APPROVAL"``) raises:
    ``AutonomyLevel._missing_`` matches case-insensitively, but the DB CHECK
    constraint (``ck_pipelines_max_autonomy_level``) is case-sensitive. This
    function's contract is raise-or-return-``None`` (it cannot hand a
    normalised value back), so it REJECTS the variant — the REST field
    validators normalise to canonical before reaching here, and a caller that
    passes the raw string through (e.g. an MCP tool) gets a clean ValueError
    instead of a stored value that would fail the CHECK with an IntegrityError
    (409) rather than the promised 422.
    """
    if ceiling is None or (lenient and not isinstance(ceiling, str)):
        return
    ceiling_level = _try_autonomy(ceiling, "max_autonomy_level")
    if ceiling_level is None:
        if lenient:
            return
        raise ValueError(f"Invalid max_autonomy_level: {ceiling!r}")
    if ceiling != ceiling_level.value:
        # FAR-1163: reject any non-canonical spelling (accepted-and-normalised
        # at the Pydantic layer; rejected here for raw-string callers).
        msg = f"max_autonomy_level must be the canonical value {ceiling_level.value!r}: {ceiling!r}"
        raise ValueError(msg)
    default_level = _try_autonomy(default if isinstance(default, str) else None, "default_autonomy_level")
    effective_default = default_level or AutonomyLevel.default()
    if autonomy_level_rank(ceiling_level) < autonomy_level_rank(effective_default):
        msg = (
            f"max_autonomy_level ({ceiling_level.value}) must be >= default_autonomy_level ({effective_default.value})"
        )
        raise ValueError(msg)


def should_skip_hitl_gate(autonomy: AutonomyLevel) -> bool:
    """Return True if the HITL gate should be bypassed at runtime.

    Resolution happens per-gate at RUNTIME (each gate re-reads the current
    run_context), not at graph-build time — the graph always contains the
    gate node; the autonomy level decides whether it interrupts.
    """
    return autonomy == AutonomyLevel.FULLY_AUTONOMOUS


def should_notify_on_complete(autonomy: AutonomyLevel) -> bool:
    """Return True if a notification event should be emitted instead of halting."""
    return autonomy == AutonomyLevel.NOTIFY_ON_COMPLETE


def autonomy_change_payload(
    previous: str | None,
    current: str | None,
) -> dict[str, str | None]:
    """Build a payload recording an autonomy level change."""
    return {
        "previous_level": previous,
        "new_level": current,
    }
