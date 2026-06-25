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
"""

from __future__ import annotations

import enum
from typing import Any


class AutonomyLevel(enum.StrEnum):
    """Autonomy level for a pipeline run's HITL gate behaviour."""

    MANUAL_APPROVAL = "manual_approval"
    NOTIFY_ON_COMPLETE = "notify_on_complete"
    FULLY_AUTONOMOUS = "fully_autonomous"

    @classmethod
    def _missing_(cls, value: object) -> AutonomyLevel:
        if isinstance(value, str):
            for member in cls:
                if member.value == value.lower().replace("-", "_"):
                    return member
        raise ValueError(f"Invalid autonomy level: {value!r}")

    @classmethod
    def default(cls) -> AutonomyLevel:
        return cls.MANUAL_APPROVAL


def effective_autonomy_level(
    pipeline_default: str | None,
    run_context: dict[str, Any] | None = None,
) -> AutonomyLevel:
    """Resolve the effective autonomy level for a run.

    Priority:
      1. ``autonomy_recommendation`` in ``run_context`` (set by a
         context-setter agent at runtime).
      2. ``pipeline_default`` (the pipeline's default_autonomy_level column).
      3. ``manual_approval`` (safest fallback).
    """
    if run_context:
        rec = run_context.get("autonomy_recommendation")
        if rec is not None:
            try:
                return AutonomyLevel(rec)
            except ValueError:
                pass
    if pipeline_default:
        try:
            return AutonomyLevel(pipeline_default)
        except ValueError:
            pass
    return AutonomyLevel.default()


def should_skip_hitl_gate(autonomy: AutonomyLevel) -> bool:
    """Return True if the HITL gate should be bypassed at graph-build time."""
    return autonomy in (AutonomyLevel.FULLY_AUTONOMOUS,)


def should_notify_on_complete(autonomy: AutonomyLevel) -> bool:
    """Return True if a notification event should be emitted instead of halting."""
    return autonomy == AutonomyLevel.NOTIFY_ON_COMPLETE


def autonomy_change_payload(
    previous: str | None,
    current: str | None,
) -> dict[str, Any]:
    return {
        "previous_level": previous,
        "new_level": current,
    }


AUTONOMY_LEVEL_VALUES = [m.value for m in AutonomyLevel]
