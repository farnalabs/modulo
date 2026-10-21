"""Judge display composition (ADR 066 §0 naming rule).

A model-backed judge is an LLM returning a number with a different meaning
from a calibrated decision model. The display must never let one be read as
the other, and the phrase "System One" is confined to the decision-model
class of the label.
"""

from __future__ import annotations

import pytest

from modulo.core.eval_engine.typed_judge import (
    SYSTEM_ONE_MODEL_LABEL,
    TYPED_QUESTIONS_MECHANISM,
    JudgeConfidenceKind,
    backing_display,
    confidence_label,
    mechanism_label,
)


def test_mechanism_label_is_shared_by_every_backing() -> None:
    assert mechanism_label() == TYPED_QUESTIONS_MECHANISM


class TestBackingDisplay:
    def test_decision_model_names_the_model_class(self) -> None:
        display = backing_display(
            kind="decision_model",
            vendor="Jev",
            confidence_kind=JudgeConfidenceKind.VENDOR_CLAIMED_CALIBRATED,
        )

        assert display.startswith(TYPED_QUESTIONS_MECHANISM)
        assert "Jev" in display
        assert SYSTEM_ONE_MODEL_LABEL in display

    @pytest.mark.parametrize(
        ("vendor", "model", "kind"),
        [
            ("Anthropic", "claude-sonnet", JudgeConfidenceKind.MODEL_SELF_REPORTED),
            (None, "llama-3.3-70b", JudgeConfidenceKind.MODEL_SELF_REPORTED),
            ("OpenAI", "gpt-4o", JudgeConfidenceKind.UNAVAILABLE),
        ],
    )
    def test_model_backed_display_never_contains_the_decision_model_phrase(
        self,
        vendor: str | None,
        model: str,
        kind: JudgeConfidenceKind,
    ) -> None:
        display = backing_display(kind="model_backend", vendor=vendor, model=model, confidence_kind=kind)

        assert display.startswith(TYPED_QUESTIONS_MECHANISM)
        assert "System One" not in display
        if kind is JudgeConfidenceKind.MODEL_SELF_REPORTED:
            assert "self-reported" in display

    def test_unknown_backing_kind_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="backing kind"):
            backing_display(kind="jev")


class TestConfidenceLabel:
    @pytest.mark.parametrize("kind", list(JudgeConfidenceKind))
    def test_every_kind_has_a_distinct_label(self, kind: JudgeConfidenceKind) -> None:
        label = confidence_label(kind)

        assert label

    def test_kinds_are_never_collapsed_into_one_label(self) -> None:
        labels = {confidence_label(kind) for kind in JudgeConfidenceKind}

        assert len(labels) == len(JudgeConfidenceKind)
