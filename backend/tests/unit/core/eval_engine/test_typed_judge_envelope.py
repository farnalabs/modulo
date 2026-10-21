"""Typed judge verdict contract (ADR 066 D5/D6/D11).

Pins the load-bearing invariants:

* ``pass``/``fail``/``error`` are the only outcomes, and ``error`` means *no
  verdict* — it always carries a code and never a score (not even ``0.0``);
* the legacy callable dialect (``passed``/``score``/``detail``) is preserved
  exactly, while the typed dialect round-trips questions and confidence
  provenance;
* a malformed/absent judge response is an error, never an inferred failure;
* typed question aggregation follows the documented weighted-mean + required
  veto + threshold rule.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from modulo.core.eval_engine.typed_judge import (
    JudgeConfidenceKind,
    JudgeErrorCode,
    JudgeExecutionError,
    JudgeOutcome,
    JudgeVerdict,
    QuestionVerdict,
    error_verdict,
    from_judge_callable_dict,
    verdict_from_typed_response,
)


class TestVerdictInvariants:
    def test_error_verdict_has_no_score_and_a_code(self) -> None:
        verdict = error_verdict(JudgeErrorCode.JUDGE_TIMEOUT, "timed out")

        assert verdict.outcome is JudgeOutcome.ERROR
        assert verdict.score is None
        assert verdict.passed is False
        assert verdict.error_code is JudgeErrorCode.JUDGE_TIMEOUT
        assert verdict.confidence_kind is JudgeConfidenceKind.UNAVAILABLE

    def test_error_outcome_requires_a_code(self) -> None:
        with pytest.raises(ValidationError, match="error_code"):
            JudgeVerdict(outcome=JudgeOutcome.ERROR, score=None)

    def test_error_outcome_rejects_a_score(self) -> None:
        with pytest.raises(ValidationError, match="score"):
            JudgeVerdict(
                outcome=JudgeOutcome.ERROR,
                score=0.0,
                error_code=JudgeErrorCode.JUDGE_UNAVAILABLE,
            )

    def test_pass_fail_may_not_carry_an_error_code(self) -> None:
        with pytest.raises(ValidationError, match="error_code"):
            JudgeVerdict(
                outcome=JudgeOutcome.FAIL,
                score=0.0,
                error_code=JudgeErrorCode.JUDGE_UNAVAILABLE,
            )


class TestErrorProvenance:
    def test_an_error_verdict_keeps_the_resolved_judge_identity(self) -> None:
        from modulo.core.eval_engine.typed_judge import JudgeIdentity

        exc = JudgeExecutionError(
            JudgeErrorCode.JUDGE_TIMEOUT,
            "timed out",
            judge=JudgeIdentity(mechanism="model_backend", judge_backend_id="backend-1"),
        )

        verdict = exc.as_verdict()

        assert verdict.outcome is JudgeOutcome.ERROR
        assert verdict.judge.judge_backend_id == "backend-1"


class TestCallableDialect:
    def test_legacy_dict_is_preserved_exactly(self) -> None:
        verdict = from_judge_callable_dict({"passed": True, "score": 0.87, "detail": "good"})

        assert verdict.outcome is JudgeOutcome.PASS
        assert verdict.score == pytest.approx(0.87)
        assert verdict.detail == "good"
        assert verdict.error_code is None
        assert verdict.confidence_kind is JudgeConfidenceKind.UNAVAILABLE

    def test_typed_dict_round_trips_questions_and_confidence(self) -> None:
        raw = JudgeVerdict(
            outcome=JudgeOutcome.FAIL,
            score=0.25,
            detail="tone",
            questions=[QuestionVerdict(question_id="tone", passed=False, confidence=0.2)],
            confidence=0.9,
            confidence_kind=JudgeConfidenceKind.MODEL_SELF_REPORTED,
            escalated=True,
        ).to_json()

        verdict = from_judge_callable_dict(raw)

        assert verdict.outcome is JudgeOutcome.FAIL
        assert verdict.questions[0].question_id == "tone"
        assert verdict.confidence_kind is JudgeConfidenceKind.MODEL_SELF_REPORTED
        assert verdict.escalated is True

    @pytest.mark.parametrize("raw_passed", ["false", "no", "False", "0"])
    def test_legacy_passed_must_be_a_real_boolean(self, raw_passed: str) -> None:
        """``bool("false")`` is True — a string must never flip a gate."""
        with pytest.raises(JudgeExecutionError) as exc_info:
            from_judge_callable_dict({"passed": raw_passed, "score": 1.0, "detail": "ok"})

        assert exc_info.value.code is JudgeErrorCode.JUDGE_MALFORMED_RESPONSE

    def test_a_non_finite_legacy_score_is_malformed(self) -> None:
        with pytest.raises(JudgeExecutionError) as exc_info:
            from_judge_callable_dict({"passed": True, "score": float("nan")})

        assert exc_info.value.code is JudgeErrorCode.JUDGE_MALFORMED_RESPONSE

    def test_non_dict_is_a_malformed_response(self) -> None:
        with pytest.raises(JudgeExecutionError) as exc_info:
            from_judge_callable_dict("not-a-dict")

        assert exc_info.value.code is JudgeErrorCode.JUDGE_MALFORMED_RESPONSE

    def test_unknown_outcome_is_a_malformed_response(self) -> None:
        with pytest.raises(JudgeExecutionError) as exc_info:
            from_judge_callable_dict({"outcome": "maybe"})

        assert exc_info.value.code is JudgeErrorCode.JUDGE_MALFORMED_RESPONSE

    def test_unknown_error_code_is_a_malformed_response(self) -> None:
        with pytest.raises(JudgeExecutionError) as exc_info:
            from_judge_callable_dict({"outcome": "error", "error_code": "judge.made_up"})

        assert exc_info.value.code is JudgeErrorCode.JUDGE_MALFORMED_RESPONSE


class TestTypedAggregation:
    def _questions(self) -> list[dict]:
        return [
            {
                "id": "grounded",
                "type": "noul",
                "weight": 1.0,
                "required": True,
                "min_pass_confidence": 0.6,
                "instructions": "grounded?",
            },
            {
                "id": "tone",
                "type": "choice",
                "options": [{"key": "neutral"}, {"key": "informal"}],
                "passing_keys": ["neutral"],
                "weight": 0.5,
                "instructions": "tone?",
            },
            {
                "id": "quality",
                "type": "score",
                "levels": [{"score": 1}, {"score": 3}, {"score": 5}],
                "pass_from": 3,
                "weight": 1.0,
                "instructions": "quality?",
            },
        ]

    def _response(self, answers: dict, detail: str = "ok") -> str:
        import json

        return json.dumps({"answers": answers, "detail": detail})

    def test_passes_when_all_required_questions_pass(self) -> None:
        text = self._response({"grounded": True, "tone": "neutral", "quality": 4})

        verdict = verdict_from_typed_response(
            text,
            questions=self._questions(),
            pass_rule={"kind": "weighted_score_at_least", "threshold": 0.6},
        )

        assert verdict.outcome is JudgeOutcome.PASS
        assert verdict.score is not None
        assert len(verdict.questions) == 3

    def test_a_failed_required_question_vetoes_the_verdict(self) -> None:
        text = self._response({"grounded": False, "tone": "neutral", "quality": 5})

        verdict = verdict_from_typed_response(
            text,
            questions=self._questions(),
            pass_rule={"kind": "weighted_score_at_least", "threshold": 0.1},
        )

        assert verdict.outcome is JudgeOutcome.FAIL

    def test_a_missing_answer_is_a_malformed_response(self) -> None:
        text = self._response({"grounded": True, "tone": "neutral"})

        with pytest.raises(JudgeExecutionError) as exc_info:
            verdict_from_typed_response(
                text,
                questions=self._questions(),
                pass_rule={"kind": "weighted_score_at_least", "threshold": 0.5},
            )

        assert exc_info.value.code is JudgeErrorCode.JUDGE_MALFORMED_RESPONSE

    @pytest.mark.parametrize("answer", ["false", "no", "False", "0"])
    def test_noul_answer_must_be_a_real_boolean(self, answer: str) -> None:
        text = self._response({"grounded": answer, "tone": "neutral", "quality": 4})

        with pytest.raises(JudgeExecutionError) as exc_info:
            verdict_from_typed_response(
                text,
                questions=self._questions(),
                pass_rule={"kind": "weighted_score_at_least", "threshold": 0.5},
            )

        assert exc_info.value.code is JudgeErrorCode.JUDGE_MALFORMED_RESPONSE

    @pytest.mark.parametrize(
        "pass_rule",
        [None, {}, {"kind": "weighted_score_at_least"}, {"threshold": "0.5"}, {"threshold": 1.5}],
    )
    def test_a_missing_or_invalid_threshold_is_a_configuration_error(self, pass_rule: Any) -> None:
        """Fail closed: a missing threshold must not make every aggregate pass."""
        text = self._response({"grounded": True, "tone": "neutral", "quality": 4})

        with pytest.raises(JudgeExecutionError) as exc_info:
            verdict_from_typed_response(text, questions=self._questions(), pass_rule=pass_rule)

        assert exc_info.value.code is JudgeErrorCode.JUDGE_CONFIGURATION

    def test_a_failed_required_question_vetoes_a_high_normalised_score(self) -> None:
        """Sage's reproduction: passed=False, confidence 0.778, previously PASS.

        The old veto compared the normalised score to min_pass_confidence
        (0.778 >= 0.6 → no veto) and summed the required question into the
        mean, so a required question below pass_from still produced PASS.
        """
        questions = [
            {
                "id": "quality",
                "type": "score",
                "levels": [{"score": n} for n in range(1, 11)],
                "pass_from": 9,
                "weight": 1.0,
                "required": True,
                "min_pass_confidence": 0.6,
                "instructions": "quality?",
            }
        ]
        text = self._response({"quality": 8})

        verdict = verdict_from_typed_response(
            text,
            questions=questions,
            pass_rule={"kind": "weighted_score_at_least", "threshold": 0.5},
        )

        assert verdict.questions[0].passed is False
        assert verdict.questions[0].confidence == pytest.approx(7 / 9)
        assert verdict.outcome is JudgeOutcome.FAIL

    def test_nan_in_a_judge_response_is_malformed(self) -> None:
        text = '{"answers": {"grounded": NaN, "tone": "neutral", "quality": 4}}'

        with pytest.raises(JudgeExecutionError) as exc_info:
            verdict_from_typed_response(
                text,
                questions=self._questions(),
                pass_rule={"kind": "weighted_score_at_least", "threshold": 0.5},
            )

        assert exc_info.value.code is JudgeErrorCode.JUDGE_MALFORMED_RESPONSE

    def test_infinity_in_a_judge_response_is_malformed(self) -> None:
        text = '{"answers": {"grounded": true, "tone": "neutral", "quality": Infinity}}'

        with pytest.raises(JudgeExecutionError) as exc_info:
            verdict_from_typed_response(
                text,
                questions=self._questions(),
                pass_rule={"kind": "weighted_score_at_least", "threshold": 0.5},
            )

        assert exc_info.value.code is JudgeErrorCode.JUDGE_MALFORMED_RESPONSE

    def test_an_all_required_set_passes_on_the_required_questions_alone(self) -> None:
        questions = [
            {"id": "a", "type": "noul", "required": True, "weight": 1.0},
            {"id": "b", "type": "noul", "required": True, "weight": 1.0},
        ]

        verdict = verdict_from_typed_response(
            self._response({"a": True, "b": True}),
            questions=questions,
            pass_rule={"kind": "weighted_score_at_least", "threshold": 1.0},
        )

        assert verdict.outcome is JudgeOutcome.PASS
        assert verdict.score is None

    def test_a_required_question_is_excluded_from_the_weighted_mean(self) -> None:
        """A passing required question at a low normalised score must not drag
        the mean below the threshold (D4: the mean is non-required only)."""
        questions = [
            {
                "id": "required",
                "type": "score",
                "levels": [{"score": n} for n in range(1, 11)],
                "pass_from": 2,
                "weight": 1.0,
                "required": True,
            },
            {"id": "optional", "type": "noul", "required": False, "weight": 1.0},
        ]

        verdict = verdict_from_typed_response(
            self._response({"required": 2, "optional": True}),
            questions=questions,
            pass_rule={"kind": "weighted_score_at_least", "threshold": 0.9},
        )

        assert verdict.questions[0].passed is True
        assert verdict.questions[0].confidence == pytest.approx(1 / 9)
        assert verdict.score == pytest.approx(1.0)
        assert verdict.outcome is JudgeOutcome.PASS

    def test_non_json_response_is_a_malformed_response(self) -> None:
        with pytest.raises(JudgeExecutionError) as exc_info:
            verdict_from_typed_response(
                "not json",
                questions=self._questions(),
                pass_rule={"kind": "weighted_score_at_least", "threshold": 0.5},
            )

        assert exc_info.value.code is JudgeErrorCode.JUDGE_MALFORMED_RESPONSE
