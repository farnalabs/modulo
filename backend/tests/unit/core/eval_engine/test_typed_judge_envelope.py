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

    def test_non_json_response_is_a_malformed_response(self) -> None:
        with pytest.raises(JudgeExecutionError) as exc_info:
            verdict_from_typed_response(
                "not json",
                questions=self._questions(),
                pass_rule={"kind": "weighted_score_at_least", "threshold": 0.5},
            )

        assert exc_info.value.code is JudgeErrorCode.JUDGE_MALFORMED_RESPONSE
