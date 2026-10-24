"""Unit tests for guardrail mute policy (FAR-2028).

Guardrails use the same mute policy as evals since they are
eval definitions with eval_type='guardrail'.
"""

from modulo.core.eval.mute_policy import (
    DEFAULT_MUTE_WINDOW_DAYS,
    evaluate_mute_policy_for_definition,
    get_definitions_near_mute_threshold,
    get_muted_definitions,
    record_mute_audit_event,
)


class TestGuardrailMutePolicyReexports:
    """Test that guardrail module re-exports eval mute policy functions."""

    def test_default_mute_window_days_reexported(self):
        from modulo.core.guardrails.mute_policy import DEFAULT_MUTE_WINDOW_DAYS as GR_WINDOW

        assert GR_WINDOW == DEFAULT_MUTE_WINDOW_DAYS
        assert GR_WINDOW == 30

    def test_evaluate_mute_policy_for_definition_reexported(self):
        from modulo.core.guardrails.mute_policy import evaluate_mute_policy_for_definition as gr_eval

        assert gr_eval is evaluate_mute_policy_for_definition

    def test_get_definitions_near_mute_threshold_reexported(self):
        from modulo.core.guardrails.mute_policy import get_definitions_near_mute_threshold as gr_get

        assert gr_get is get_definitions_near_mute_threshold

    def test_get_muted_definitions_reexported(self):
        from modulo.core.guardrails.mute_policy import get_muted_definitions as gr_get

        assert gr_get is get_muted_definitions

    def test_record_mute_audit_event_reexported(self):
        from modulo.core.guardrails.mute_policy import record_mute_audit_event as gr_record

        assert gr_record is record_mute_audit_event

    def test_all_exports_present(self):
        from modulo.core.guardrails.mute_policy import __all__

        expected = [
            "DEFAULT_MUTE_WINDOW_DAYS",
            "evaluate_mute_policy_for_definition",
            "get_definitions_near_mute_threshold",
            "get_muted_definitions",
            "record_mute_audit_event",
        ]
        assert set(__all__) == set(expected)
