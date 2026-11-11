"""Unit tests for guardrail finding resolver (FAR-2028).

Guardrails use the same finding resolver as evals since they are
eval definitions with eval_type='guardrail'.
"""

import pytest

from farna.core.eval import finding_resolver as eval_finding_resolver
from farna.core.guardrails import finding_resolver as guardrail_finding_resolver

EXPECTED_EXPORTS = [
    "RESOLUTION_VALUES",
    "FindingResolutionError",
    "apply_mute_policy",
    "compute_acceptance_rate",
    "evaluate_mute_policy",
    "resolve_finding",
    "unmute_definition",
]


@pytest.mark.parametrize("name", [n for n in EXPECTED_EXPORTS if n != "RESOLUTION_VALUES"])
def test_guardrail_finding_resolver_reexports_name_by_identity(name):
    """The guardrail module must re-export the eval resolver object itself."""
    assert getattr(guardrail_finding_resolver, name) is getattr(eval_finding_resolver, name)


def test_resolution_values_is_the_eval_tuple():
    assert guardrail_finding_resolver.RESOLUTION_VALUES is eval_finding_resolver.RESOLUTION_VALUES
    assert guardrail_finding_resolver.RESOLUTION_VALUES == ("accepted", "dismissed", "superseded", "pending")


def test_all_exports_present():
    assert set(guardrail_finding_resolver.__all__) == set(EXPECTED_EXPORTS)
