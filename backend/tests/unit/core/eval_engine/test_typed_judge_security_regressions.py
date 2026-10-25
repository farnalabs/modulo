"""End-to-end regressions for the verdict gate (ADR 066 S2 fix round).

Each case drives the REAL transport (``llm_judge`` → ``typed_judge.transport``)
and the real aggregation, not a hand-built verdict, because every one of these
was a gate hole that unit-level helpers alone did not catch.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from langchain_core.messages import AIMessage

from modulo.core.eval_engine import EvalDefinition, EvalEngine, EvalType
from modulo.core.eval_engine.typed_judge import JudgeErrorCode, JudgeOutcome
from modulo.core.pipeline_engine import llm_judge

_BACKEND_ID = uuid.uuid4()


class _FakeBackend:
    def __init__(self, content: str) -> None:
        self._content = content
        self.calls = 0

    async def invoke(self, messages: list[Any], **_: Any) -> AIMessage:
        self.calls += 1
        return AIMessage(content=self._content)


class _FakeHub:
    def __init__(self, backend: _FakeBackend) -> None:
        self._backend = backend

    async def get(self, backend_id: uuid.UUID, **_: Any) -> _FakeBackend:
        return self._backend


def _eval_def(question: dict[str, Any], **config: Any) -> EvalDefinition:
    return EvalDefinition(
        id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        name="typed-judge",
        eval_type=EvalType.MODEL_JUDGE,
        config={
            "answer_contract": "typed_questions",
            "field": "output",
            "backing": {"kind": "model_backend", "model_backend_id": str(_BACKEND_ID)},
            "questions": [question],
            "pass_rule": {"kind": "weighted_score_at_least", "threshold": 0.5},
            **config,
        },
        failure_behaviour="warn",
    )


def _judge(content: str) -> tuple[Any, _FakeBackend]:
    backend = _FakeBackend(content)
    return llm_judge.build_model_judge_callable(_FakeHub(backend), str(_BACKEND_ID)), backend


def _noul() -> dict[str, Any]:
    return {"id": "q", "type": "noul", "weight": 1.0, "required": True, "min_pass_confidence": 0.6}


def _score() -> dict[str, Any]:
    return {"id": "q", "type": "score", "levels": [{"score": 0}, {"score": 1}], "pass_from": 0.5, "weight": 1.0}


def _choice() -> dict[str, Any]:
    return {
        "id": "q",
        "type": "choice",
        "options": [{"key": "a"}, {"key": "b"}],
        "passing_keys": ["a"],
        "weight": 1.0,
    }


class TestWholeOutputBoundRegression:
    def test_an_over_bound_whole_output_is_an_error_and_is_never_sent(self) -> None:
        judge, backend = _judge("unused")
        eval_def = _eval_def(_noul(), judge_whole_output=True)
        eval_def.config.pop("field", None)

        result = EvalEngine().evaluate_result(
            {"payload": "x" * 100_001},
            eval_def,
            model_judge_callable=judge,
        )

        assert result.outcome is JudgeOutcome.ERROR
        assert result.judge_verdict is not None
        assert result.judge_verdict.error_code is JudgeErrorCode.JUDGE_CONFIGURATION
        assert backend.calls == 0, "an over-bound payload must never reach the provider"


class TestNumericCoercionRegression:
    def test_a_boolean_score_answer_cannot_clear_the_pass_level(self) -> None:
        judge, _ = _judge(json.dumps({"answer": True}))

        result = EvalEngine().evaluate_result({"output": "x"}, _eval_def(_score()), model_judge_callable=judge)

        assert result.outcome is JudgeOutcome.ERROR
        assert result.judge_verdict is not None
        assert result.judge_verdict.error_code is JudgeErrorCode.JUDGE_MALFORMED_RESPONSE

    def test_a_boolean_probability_cannot_clear_a_veto(self) -> None:
        judge, _ = _judge(json.dumps({"answer": True, "probabilities": {"yes": True, "no": 0.0}}))

        result = EvalEngine().evaluate_result({"output": "x"}, _eval_def(_noul()), model_judge_callable=judge)

        assert result.outcome is JudgeOutcome.ERROR
        assert result.judge_verdict is not None
        assert result.judge_verdict.error_code is JudgeErrorCode.JUDGE_MALFORMED_RESPONSE


class TestPartialDistributionRegression:
    @pytest.mark.parametrize(
        ("question", "payload"),
        [
            (_noul(), {"answer": True, "probabilities": {"yes": 0.05}}),
            (_choice(), {"answer": "a", "probabilities": {"a": 0.9}}),
            (
                {"id": "q", "type": "score", "levels": [{"score": 1}, {"score": 3}], "pass_from": 3, "weight": 1.0},
                {"answer": 3, "probabilities": {"3": 0.9}},
            ),
        ],
        ids=["noul", "choice", "score"],
    )
    def test_a_partial_distribution_errors_rather_than_renormalising(
        self, question: dict[str, Any], payload: dict[str, Any]
    ) -> None:
        judge, _ = _judge(json.dumps(payload))

        result = EvalEngine().evaluate_result({"output": "x"}, _eval_def(question), model_judge_callable=judge)

        assert result.outcome is JudgeOutcome.ERROR
        assert result.judge_verdict is not None
        assert result.judge_verdict.error_code is JudgeErrorCode.JUDGE_MALFORMED_RESPONSE


class TestAnswerVocabularyRegression:
    def test_an_out_of_vocabulary_choice_answer_errors_rather_than_failing(self) -> None:
        judge, _ = _judge(json.dumps({"answer": "c"}))

        result = EvalEngine().evaluate_result({"output": "x"}, _eval_def(_choice()), model_judge_callable=judge)

        assert result.outcome is JudgeOutcome.ERROR
        assert result.judge_verdict is not None
        assert result.judge_verdict.error_code is JudgeErrorCode.JUDGE_MALFORMED_RESPONSE
