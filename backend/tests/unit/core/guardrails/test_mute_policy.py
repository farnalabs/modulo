"""Unit tests for guardrail mute policy (FAR-2028).

Guardrails use the same mute policy as evals since they are
eval definitions with eval_type='guardrail'.
"""

import pytest

from farna.core.eval import mute_policy as eval_mute_policy
from farna.core.guardrails import mute_policy as guardrail_mute_policy

EXPECTED_EXPORTS = [
    "DEFAULT_MUTE_WINDOW_DAYS",
    "evaluate_mute_policy_for_definition",
    "get_definitions_near_mute_threshold",
    "get_muted_definitions",
    "record_mute_audit_event",
]


@pytest.mark.parametrize("name", [n for n in EXPECTED_EXPORTS if n != "DEFAULT_MUTE_WINDOW_DAYS"])
def test_guardrail_mute_policy_reexports_name_by_identity(name):
    """The guardrail module must re-export the eval mute policy object itself."""
    assert getattr(guardrail_mute_policy, name) is getattr(eval_mute_policy, name)


def test_default_mute_window_days_is_thirty_days():
    assert guardrail_mute_policy.DEFAULT_MUTE_WINDOW_DAYS is eval_mute_policy.DEFAULT_MUTE_WINDOW_DAYS
    assert guardrail_mute_policy.DEFAULT_MUTE_WINDOW_DAYS == 30


def test_all_exports_present():
    assert set(guardrail_mute_policy.__all__) == set(EXPECTED_EXPORTS)
