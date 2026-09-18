"""Run context — seeded at run start from pipeline defaults and extended by context-setter agents during execution.

The autonomy module provides runtime resolution of HITL gate behaviour
based on pipeline-level configuration and context-setter recommendations.
"""

from modulo.core.run_context.autonomy import (
    AUTONOMY_LEVEL_VALUES,
    AutonomyLevel,
    autonomy_change_payload,
    effective_autonomy_level,
    should_notify_on_complete,
    should_skip_hitl_gate,
)

__all__ = [
    "AUTONOMY_LEVEL_VALUES",
    "AutonomyLevel",
    "autonomy_change_payload",
    "effective_autonomy_level",
    "should_notify_on_complete",
    "should_skip_hitl_gate",
]
