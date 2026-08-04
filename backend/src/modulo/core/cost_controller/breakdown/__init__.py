"""Multi-component cost breakdown package (PR A1 — data model + engine half).

This package is the backend data-model + engine half of the cost-tracking
feature. The executor finalization + ledger + probe half lands in PR A2.

Modules:
- ``constants`` — shared limits/constants (defaults only; runtime reads flow
  through ``get_settings()`` so an env override moves the boundary everywhere).
- ``formula`` — the 4-operator (``+ - * /``) stdlib formula tokenizer, parser,
  evaluator, and ``CostFormulaError``. No eval/exec, no third-party evaluator.
- ``params`` — the param registry (the fixed identifier surface operators may
  reference) and the ``RunCostTelemetry`` builder.
- ``aggregate`` — ``build_cost_breakdown`` (the single function that computes
  the breakdown + total together, preserving ``total == sum``).
"""

from modulo.core.cost_controller.breakdown.aggregate import (
    build_cost_breakdown,
    clamp_reported,
    clamp_to_ceiling,
)
from modulo.core.cost_controller.breakdown.constants import (
    MAX_BREAKDOWN_BASIS_SIZE,
    MAX_COMPONENTS_PER_ORG,
    MAX_DISPLAY_NAME_LENGTH,
    MAX_FORMULA_DEPTH,
    MAX_FORMULA_LENGTH,
    MAX_NAME_LENGTH,
    MAX_RATE_USD,
    MAX_REPORTABLE_BAND_USD,
    MAX_REPORTABLE_USD_MIN,
    MAX_SELF_REPORTED_USD,
    NODE_TYPE_SANDBOX_AGENT,
    PLAUSIBLE_NODE_COUNT,
)
from modulo.core.cost_controller.breakdown.formula import (
    CostFormulaError,
    evaluate_formula,
    validate_formula,
)
from modulo.core.cost_controller.breakdown.params import (
    REGISTERED_RATE_FALLBACKS,
    CostComponentConfig,
    RunCostTelemetry,
    build_telemetry,
)

__all__ = [
    "MAX_BREAKDOWN_BASIS_SIZE",
    "MAX_COMPONENTS_PER_ORG",
    "MAX_DISPLAY_NAME_LENGTH",
    "MAX_FORMULA_DEPTH",
    "MAX_FORMULA_LENGTH",
    "MAX_NAME_LENGTH",
    "MAX_RATE_USD",
    "MAX_REPORTABLE_BAND_USD",
    "MAX_REPORTABLE_USD_MIN",
    "MAX_SELF_REPORTED_USD",
    "NODE_TYPE_SANDBOX_AGENT",
    "PLAUSIBLE_NODE_COUNT",
    "REGISTERED_RATE_FALLBACKS",
    "CostComponentConfig",
    "CostFormulaError",
    "RunCostTelemetry",
    "build_cost_breakdown",
    "build_telemetry",
    "clamp_reported",
    "clamp_to_ceiling",
    "evaluate_formula",
    "validate_formula",
]
