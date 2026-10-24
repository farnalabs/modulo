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

    def test_a_non_numeric_legacy_score_is_dropped(self) -> None:
        verdict = from_judge_callable_dict({"passed": True, "score": "not-a-number"})

        assert verdict.score is None

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


class TestDialectCoercionEdges:
    """Every malformed envelope field is a coded error, never an inferred verdict."""

    def test_an_explicit_judge_identity_wins_over_the_payload(self) -> None:
        from modulo.core.eval_engine.typed_judge import JudgeIdentity

        verdict = from_judge_callable_dict(
            {"passed": True, "judge": {"judge_backend_id": "payload"}},
            judge=JudgeIdentity(judge_backend_id="explicit"),
        )

        assert verdict.judge.judge_backend_id == "explicit"

    def test_a_non_dict_judge_payload_defaults_the_identity(self) -> None:
        verdict = from_judge_callable_dict({"passed": True, "judge": "not-a-dict"})

        assert verdict.judge.mechanism == "model_backend"

    def test_an_invalid_judge_identity_is_malformed(self) -> None:
        with pytest.raises(JudgeExecutionError) as exc_info:
            from_judge_callable_dict({"passed": True, "judge": {"bogus": 1}})

        assert exc_info.value.code is JudgeErrorCode.JUDGE_MALFORMED_RESPONSE

    def test_an_unknown_confidence_kind_is_malformed(self) -> None:
        with pytest.raises(JudgeExecutionError) as exc_info:
            from_judge_callable_dict({"outcome": "pass", "confidence_kind": "invented"})

        assert exc_info.value.code is JudgeErrorCode.JUDGE_MALFORMED_RESPONSE

    def test_a_null_score_is_absent_not_zero(self) -> None:
        verdict = from_judge_callable_dict({"outcome": "pass", "score": None})

        assert verdict.score is None

    def test_a_null_detail_becomes_an_empty_string(self) -> None:
        typed = from_judge_callable_dict({"outcome": "pass", "detail": None})
        legacy = from_judge_callable_dict({"passed": True, "detail": None})

        assert not typed.detail
        assert not legacy.detail

    def test_the_question_list_defaults_to_empty(self) -> None:
        verdict = from_judge_callable_dict({"outcome": "pass"})

        assert not verdict.questions

    def test_a_non_list_question_field_is_malformed(self) -> None:
        with pytest.raises(JudgeExecutionError) as exc_info:
            from_judge_callable_dict({"outcome": "pass", "questions": "one"})

        assert exc_info.value.code is JudgeErrorCode.JUDGE_MALFORMED_RESPONSE

    def test_an_invalid_question_record_is_malformed(self) -> None:
        with pytest.raises(JudgeExecutionError) as exc_info:
            from_judge_callable_dict({"outcome": "pass", "questions": [{"bogus": 1}]})

        assert exc_info.value.code is JudgeErrorCode.JUDGE_MALFORMED_RESPONSE

    def test_an_error_verdict_with_a_score_is_malformed(self) -> None:
        """The envelope invariant is enforced at construction, then coded."""
        with pytest.raises(JudgeExecutionError) as exc_info:
            from_judge_callable_dict({"outcome": "error", "score": 0.5, "error_code": "judge.timeout"})

        assert exc_info.value.code is JudgeErrorCode.JUDGE_MALFORMED_RESPONSE


class TestQuestionScoringEdges:
    """The per-type scorer's malformed-answer and option-score branches."""

    def _aggregate(
        self,
        questions: list[dict[str, Any]],
        answers: dict[str, Any],
        threshold: float = 0.5,
    ) -> tuple[list[QuestionVerdict], float | None, bool]:
        from modulo.core.eval_engine.typed_judge import aggregate_typed_answers

        return aggregate_typed_answers(
            questions,
            answers,
            {"kind": "weighted_score_at_least", "threshold": threshold},
        )

    def test_a_choice_option_score_overrides_the_binary_score(self) -> None:
        questions = [
            {
                "id": "tone",
                "type": "choice",
                "options": [{"key": "a"}, {"key": "b"}],
                "passing_keys": ["a"],
                "option_scores": {"a": 0.4, "b": 0.1},
                "weight": 1.0,
            }
        ]

        verdicts, aggregate, passed = self._aggregate(questions, {"tone": "a"}, threshold=0.2)

        assert verdicts[0].passed is True
        assert verdicts[0].confidence == pytest.approx(0.4)
        assert aggregate == pytest.approx(0.4)
        assert passed is True

    def test_a_null_option_score_falls_back_to_the_binary_score(self) -> None:
        questions = [
            {
                "id": "tone",
                "type": "choice",
                "options": [{"key": "a"}, {"key": "b"}],
                "passing_keys": ["a"],
                "option_scores": {"a": None},
                "weight": 1.0,
            }
        ]

        verdicts, _, _ = self._aggregate(questions, {"tone": "a"})

        assert verdicts[0].confidence == pytest.approx(1.0)

    def test_a_non_list_passing_keys_is_a_configuration_error(self) -> None:
        questions = [{"id": "tone", "type": "choice", "passing_keys": "a", "options": []}]

        with pytest.raises(JudgeExecutionError) as exc_info:
            self._aggregate(questions, {"tone": "a"})

        assert exc_info.value.code is JudgeErrorCode.JUDGE_CONFIGURATION

    def test_non_dict_levels_are_skipped_then_too_few_levels_is_a_config_error(self) -> None:
        questions = [
            {
                "id": "quality",
                "type": "score",
                "levels": [{"score": 1}, "junk"],
                "pass_from": 1,
                "weight": 1.0,
            }
        ]

        with pytest.raises(JudgeExecutionError) as exc_info:
            self._aggregate(questions, {"quality": 1})

        assert exc_info.value.code is JudgeErrorCode.JUDGE_CONFIGURATION

    def test_a_null_level_score_is_skipped(self) -> None:
        questions = [
            {
                "id": "quality",
                "type": "score",
                "levels": [{"score": None}, {"score": 2}, {"score": 4}],
                "pass_from": 2,
                "weight": 1.0,
            }
        ]

        verdicts, _, passed = self._aggregate(questions, {"quality": 2}, threshold=1.0)

        assert verdicts[0].passed is True
        assert verdicts[0].confidence == pytest.approx(0.0)
        assert passed is False

    def test_equal_level_bounds_normalise_to_zero(self) -> None:
        questions = [
            {
                "id": "quality",
                "type": "score",
                "levels": [{"score": 5}, {"score": 5}],
                "pass_from": 5,
                "weight": 1.0,
            }
        ]

        verdicts, _, passed = self._aggregate(questions, {"quality": 5}, threshold=0.0)

        assert verdicts[0].confidence == pytest.approx(0.0)
        assert passed is True

    def test_a_null_score_answer_is_a_malformed_response(self) -> None:
        questions = [
            {
                "id": "quality",
                "type": "score",
                "levels": [{"score": 1}, {"score": 5}],
                "pass_from": 3,
                "weight": 1.0,
            }
        ]

        with pytest.raises(JudgeExecutionError) as exc_info:
            self._aggregate(questions, {"quality": None})

        assert exc_info.value.code is JudgeErrorCode.JUDGE_MALFORMED_RESPONSE

    def test_a_non_numeric_score_answer_is_a_malformed_response(self) -> None:
        questions = [
            {
                "id": "quality",
                "type": "score",
                "levels": [{"score": 1}, {"score": 5}],
                "pass_from": 3,
                "weight": 1.0,
            }
        ]

        with pytest.raises(JudgeExecutionError) as exc_info:
            self._aggregate(questions, {"quality": "high"})

        assert exc_info.value.code is JudgeErrorCode.JUDGE_MALFORMED_RESPONSE

    def test_a_missing_pass_from_is_a_configuration_error(self) -> None:
        questions = [
            {
                "id": "quality",
                "type": "score",
                "levels": [{"score": 1}, {"score": 5}],
                "weight": 1.0,
            }
        ]

        with pytest.raises(JudgeExecutionError) as exc_info:
            self._aggregate(questions, {"quality": 3})

        assert exc_info.value.code is JudgeErrorCode.JUDGE_CONFIGURATION

    def test_an_unsupported_question_type_is_a_configuration_error(self) -> None:
        questions = [{"id": "odd", "type": "matches", "weight": 1.0}]

        with pytest.raises(JudgeExecutionError) as exc_info:
            self._aggregate(questions, {"odd": "yes"})

        assert exc_info.value.code is JudgeErrorCode.JUDGE_CONFIGURATION

    def test_a_question_without_an_id_is_a_configuration_error(self) -> None:
        questions = [{"type": "noul", "weight": 1.0}]

        with pytest.raises(JudgeExecutionError) as exc_info:
            self._aggregate(questions, {})

        assert exc_info.value.code is JudgeErrorCode.JUDGE_CONFIGURATION

    def test_a_null_question_id_is_labelled_unknown_in_an_error(self) -> None:
        with pytest.raises(JudgeExecutionError) as exc_info:
            self._aggregate([{"id": None, "type": "noul", "weight": 1.0}], {"None": "yes"})

        assert exc_info.value.code is JudgeErrorCode.JUDGE_MALFORMED_RESPONSE
        assert "'?'" in exc_info.value.detail

    def test_a_non_primitive_answer_is_not_recorded(self) -> None:
        questions = [
            {
                "id": "tone",
                "type": "choice",
                "options": [{"key": "a"}, {"key": "b"}],
                "passing_keys": ["a"],
                "weight": 1.0,
            }
        ]

        verdicts, _, _ = self._aggregate(questions, {"tone": ("a",)})

        assert verdicts[0].answer is None

    def test_a_null_weight_is_a_configuration_error(self) -> None:
        questions = [{"id": "optional", "type": "noul", "weight": None}]

        with pytest.raises(JudgeExecutionError) as exc_info:
            self._aggregate(questions, {"optional": True})

        assert exc_info.value.code is JudgeErrorCode.JUDGE_CONFIGURATION

    def test_a_negative_weight_is_a_configuration_error(self) -> None:
        with pytest.raises(JudgeExecutionError) as exc_info:
            self._aggregate([{"id": "q", "type": "noul", "weight": -1}], {"q": True})

        assert exc_info.value.code is JudgeErrorCode.JUDGE_CONFIGURATION

    def test_a_non_numeric_weight_is_a_configuration_error(self) -> None:
        with pytest.raises(JudgeExecutionError) as exc_info:
            self._aggregate([{"id": "q", "type": "noul", "weight": "heavy"}], {"q": True})

        assert exc_info.value.code is JudgeErrorCode.JUDGE_CONFIGURATION

    def test_a_non_finite_weight_is_a_configuration_error(self) -> None:
        with pytest.raises(JudgeExecutionError) as exc_info:
            self._aggregate([{"id": "q", "type": "noul", "weight": float("nan")}], {"q": True})

        assert exc_info.value.code is JudgeErrorCode.JUDGE_CONFIGURATION

    def test_a_zero_weight_question_is_excluded_from_the_mean(self) -> None:
        questions = [{"id": "q", "type": "noul", "weight": 0}]

        verdicts, aggregate, passed = self._aggregate(questions, {"q": True})

        assert aggregate is None
        assert passed is True
        assert verdicts[0].passed is True

    def test_a_nan_threshold_is_a_configuration_error(self) -> None:
        with pytest.raises(JudgeExecutionError) as exc_info:
            self._aggregate([{"id": "q", "type": "noul", "weight": 1.0}], {"q": True}, threshold=float("nan"))

        assert exc_info.value.code is JudgeErrorCode.JUDGE_CONFIGURATION


class TestTypedResponseParsing:
    def test_a_response_without_an_answers_object_is_malformed(self) -> None:
        with pytest.raises(JudgeExecutionError) as exc_info:
            verdict_from_typed_response(
                "{}",
                questions=[],
                pass_rule={"kind": "weighted_score_at_least", "threshold": 0.5},
            )

        assert exc_info.value.code is JudgeErrorCode.JUDGE_MALFORMED_RESPONSE

    def test_a_non_object_response_is_malformed(self) -> None:
        with pytest.raises(JudgeExecutionError) as exc_info:
            verdict_from_typed_response(
                "[]",
                questions=[],
                pass_rule={"kind": "weighted_score_at_least", "threshold": 0.5},
            )

        assert exc_info.value.code is JudgeErrorCode.JUDGE_MALFORMED_RESPONSE


class TestPromptBuilder:
    def test_the_prompt_embeds_every_question_and_the_pass_rule(self) -> None:
        from modulo.core.eval_engine.typed_judge import build_typed_judge_prompt

        prompt = build_typed_judge_prompt(
            [{"id": "tone", "type": "noul", "instructions": "neutral?", "criteria": ["calm"]}],
            {"kind": "weighted_score_at_least", "threshold": 0.5},
            "the output",
        )

        assert '"id": "tone"' in prompt
        assert '"threshold": 0.5' in prompt
        assert "the output" in prompt

    def test_a_choice_question_embeds_its_option_keys_and_passing_keys(self) -> None:
        from modulo.core.eval_engine.typed_judge import build_typed_judge_prompt

        prompt = build_typed_judge_prompt(
            [
                {
                    "id": "verdict",
                    "type": "choice",
                    "instructions": "pick one",
                    "options": [
                        {"key": "approve", "description": "ship it"},
                        {"key": "reject", "description": "block it"},
                        "not-an-option",
                    ],
                    "passing_keys": ["approve"],
                }
            ],
            None,
            "the output",
        )

        assert '"key": "approve"' in prompt
        assert '"key": "reject"' in prompt
        assert '"description": "ship it"' in prompt
        assert '"passing_keys": ["approve"]' in prompt
        assert "not-an-option" not in prompt

    def test_a_choice_question_without_a_declared_answer_space_still_renders(self) -> None:
        from modulo.core.eval_engine.typed_judge import build_typed_judge_prompt

        prompt = build_typed_judge_prompt([{"id": "verdict", "type": "choice"}], None, "the output")

        assert '"id": "verdict"' in prompt
        assert '"options"' not in prompt
        assert '"passing_keys"' not in prompt

    def test_a_score_question_embeds_its_level_scores_and_pass_from(self) -> None:
        from modulo.core.eval_engine.typed_judge import build_typed_judge_prompt

        prompt = build_typed_judge_prompt(
            [
                {
                    "id": "quality",
                    "type": "score",
                    "instructions": "how good?",
                    "levels": [
                        {"score": 0, "description": "poor"},
                        {"score": 1, "description": "great"},
                        "not-a-level",
                    ],
                    "pass_from": 1,
                }
            ],
            None,
            "the output",
        )

        assert '"score": 0' in prompt
        assert '"score": 1' in prompt
        assert '"description": "great"' in prompt
        assert '"pass_from": 1' in prompt
        assert "not-a-level" not in prompt

    def test_a_score_question_without_declared_levels_still_renders(self) -> None:
        from modulo.core.eval_engine.typed_judge import build_typed_judge_prompt

        prompt = build_typed_judge_prompt(
            [{"id": "quality", "type": "score", "pass_from": 1}],
            None,
            "the output",
        )

        assert '"id": "quality"' in prompt
        assert '"levels"' not in prompt
        assert '"pass_from": 1' in prompt
