"""Unit tests for guardrail finding resolver (FAR-2028).

Guardrails use the same finding resolver as evals since they are
eval definitions with eval_type='guardrail'.
"""

from modulo.core.eval.finding_resolver import (
    RESOLUTION_VALUES,
    FindingResolutionError,
    apply_mute_policy,
    compute_acceptance_rate,
    evaluate_mute_policy,
    resolve_finding,
    unmute_definition,
)


class TestGuardrailFindingResolverReexports:
    """Test that guardrail module re-exports eval finding resolver functions."""

    def test_resolution_values_reexported(self):
        from modulo.core.guardrails.finding_resolver import RESOLUTION_VALUES as GR_VALUES

        assert GR_VALUES == RESOLUTION_VALUES
        assert GR_VALUES == ("accepted", "dismissed", "superseded", "pending")

    def test_finding_resolution_error_reexported(self):
        from modulo.core.guardrails.finding_resolver import FindingResolutionError as GR_Error

        assert GR_Error is FindingResolutionError

    def test_resolve_finding_reexported(self):
        from modulo.core.guardrails.finding_resolver import resolve_finding as gr_resolve

        assert gr_resolve is resolve_finding

    def test_compute_acceptance_rate_reexported(self):
        from modulo.core.guardrails.finding_resolver import compute_acceptance_rate as gr_compute

        assert gr_compute is compute_acceptance_rate

    def test_evaluate_mute_policy_reexported(self):
        from modulo.core.guardrails.finding_resolver import evaluate_mute_policy as gr_eval

        assert gr_eval is evaluate_mute_policy

    def test_apply_mute_policy_reexported(self):
        from modulo.core.guardrails.finding_resolver import apply_mute_policy as gr_apply

        assert gr_apply is apply_mute_policy

    def test_unmute_definition_reexported(self):
        from modulo.core.guardrails.finding_resolver import unmute_definition as gr_unmute

        assert gr_unmute is unmute_definition

    def test_all_exports_present(self):
        from modulo.core.guardrails.finding_resolver import __all__

        expected = [
            "RESOLUTION_VALUES",
            "FindingResolutionError",
            "apply_mute_policy",
            "compute_acceptance_rate",
            "evaluate_mute_policy",
            "resolve_finding",
            "unmute_definition",
        ]
        assert set(__all__) == set(expected)
