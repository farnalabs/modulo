"""Run context — seeded at run start from pipeline defaults and extended by context-setter agents during execution.

The autonomy module provides runtime resolution of HITL gate behaviour
based on pipeline-level configuration and context-setter recommendations.
"""

from modulo.core.run_context.autonomy import (
    AUTONOMY_GATING_FLAG,
    AUTONOMY_LEVEL_VALUES,
    PIPELINE_EARNED_AT_START_KEY,
    PIPELINE_MAX_AUTONOMY_KEY,
    AutonomyLevel,
    AutonomyResolution,
    autonomy_change_payload,
    autonomy_level_rank,
    effective_autonomy_level,
    is_autonomy_gating_enabled,
    read_live_earned_autonomy,
    resolve_autonomy,
    should_notify_on_complete,
    should_skip_hitl_review,
    validate_autonomy_ceiling,
)
from modulo.core.run_context.autonomy_telemetry import (
    AUTONOMY_RECOMMENDATION_CLAMPED,
    emit_autonomy_clamp_telemetry,
)

__all__ = [
    "AUTONOMY_GATING_FLAG",
    "AUTONOMY_LEVEL_VALUES",
    "AUTONOMY_RECOMMENDATION_CLAMPED",
    "PIPELINE_EARNED_AT_START_KEY",
    "PIPELINE_MAX_AUTONOMY_KEY",
    "AutonomyLevel",
    "AutonomyResolution",
    "autonomy_change_payload",
    "autonomy_level_rank",
    "effective_autonomy_level",
    "emit_autonomy_clamp_telemetry",
    "is_autonomy_gating_enabled",
    "read_live_earned_autonomy",
    "resolve_autonomy",
    "should_notify_on_complete",
    "should_skip_hitl_review",
    "validate_autonomy_ceiling",
]
