"""Run context — seeded at run start from pipeline defaults and extended by context-setter agents during execution.

The autonomy module provides runtime resolution of HITL gate behaviour
based on pipeline-level configuration and context-setter recommendations.
"""

from modulo.core.run_context.autonomy import (
    AUTONOMY_LEVEL_VALUES,
    PIPELINE_MAX_AUTONOMY_KEY,
    AutonomyLevel,
    AutonomyResolution,
    autonomy_change_payload,
    autonomy_level_rank,
    effective_autonomy_level,
    resolve_autonomy,
    should_notify_on_complete,
    should_skip_hitl_gate,
    validate_autonomy_ceiling,
)
from modulo.core.run_context.autonomy_telemetry import (
    AUTONOMY_RECOMMENDATION_CLAMPED,
    emit_autonomy_clamp_telemetry,
)

__all__ = [
    "AUTONOMY_LEVEL_VALUES",
    "AUTONOMY_RECOMMENDATION_CLAMPED",
    "PIPELINE_MAX_AUTONOMY_KEY",
    "AutonomyLevel",
    "AutonomyResolution",
    "autonomy_change_payload",
    "autonomy_level_rank",
    "effective_autonomy_level",
    "emit_autonomy_clamp_telemetry",
    "resolve_autonomy",
    "should_notify_on_complete",
    "should_skip_hitl_gate",
    "validate_autonomy_ceiling",
]
