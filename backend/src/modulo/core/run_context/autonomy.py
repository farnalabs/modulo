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
import logging
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


def _try_autonomy(value: str | None, label: str) -> AutonomyLevel | None:
    if value is None:
        return None
    try:
        return AutonomyLevel(value)
    except ValueError:
        _log.warning("Invalid %s %r — falling back", label, value)
        return None


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
    if isinstance(run_context, dict):
        result = _try_autonomy(
            run_context.get("autonomy_recommendation"),
            "run_context autonomy_recommendation",
        )
        if result is not None:
            return result
    elif run_context is not None:
        _log.warning("run_context is not a dict (got %s), ignoring", type(run_context).__name__)
    result = _try_autonomy(pipeline_default, "pipeline_default_autonomy_level")
    if result is not None:
        return result
    return AutonomyLevel.default()


def should_skip_hitl_gate(autonomy: AutonomyLevel) -> bool:
    """Return True if the HITL gate should be bypassed at graph-build time."""
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
