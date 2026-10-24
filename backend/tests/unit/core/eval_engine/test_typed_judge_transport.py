"""Typed-question transport, deadlines, breaker and bridge (ADR 066 D9/D10).

These tests pin the behaviour the transport owns: the native-vs-embedded
schema decision, corrective retries, derived pass-confidence, per-question and
per-definition deadlines, the circuit breaker, usage capture, and the proof
that a hung call cannot wedge a later call.
"""

from __future__ import annotations

import asyncio
import json
import threading
import uuid
from collections.abc import Callable
from typing import Any

import pytest
from langchain_core.messages import AIMessage

import modulo.core.pipeline_engine.llm_judge as llm_judge
from modulo.core.eval_engine import EvalDefinition, EvalType
from modulo.core.eval_engine.typed_judge import (
    JudgeConfidenceKind,
    JudgeErrorCode,
    JudgeExecutionError,
    JudgeIdentity,
    JudgeOutcome,
    transport,
)
from modulo.core.eval_engine.typed_judge import bridge as judge_bridge
from modulo.core.eval_engine.typed_judge.transport import (
    MAX_CORRECTIVE_RETRIES,
    JudgeBackendHandle,
    judge_questions,
    reset_circuit_breaker,
)

SESSION_ONE = "Typed questions"


@pytest.fixture(autouse=True)
def _fresh_process_state():
    reset_circuit_breaker()
    judge_bridge.reset_bridge()
    yield
    reset_circuit_breaker()
    judge_bridge.reset_bridge()


class _RecordingBackend:
    """A fake model backend that records how it was invoked."""

    def __init__(
        self,
        responder: Callable[[int], str],
        *,
        supports_native_structured_output: bool = False,
        delay: float = 0.0,
    ) -> None:
        self.responder = responder
        self.supports_native_structured_output = supports_native_structured_output
        self.delay = delay
        self.calls: list[dict[str, Any]] = []
        self.in_flight = 0
        self.max_in_flight = 0

    async def invoke(self, messages: list[Any], output_schema: dict[str, Any] | None = None, **_: Any) -> AIMessage:
        index = len(self.calls)
        self.calls.append({"prompt": messages[-1].content, "schema": output_schema})
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            return AIMessage(
                content=self.responder(index),
                usage_metadata={
                    "input_tokens": 3,
                    "output_tokens": 2,
                    "total_tokens": 5,
                },
            )
        finally:
            self.in_flight -= 1


def _handle(backend: _RecordingBackend, key: str | None = None) -> JudgeBackendHandle:
    async def _invoke(messages: list[Any], output_schema: dict[str, Any] | None) -> Any:
        if output_schema is None:
            return await backend.invoke(messages)
        return await backend.invoke(messages, output_schema=output_schema)

    return JudgeBackendHandle(
        key=key or str(uuid.uuid4()),
        invoke=_invoke,
        supports_native_structured_output=backend.supports_native_structured_output,
    )


def _identity() -> JudgeIdentity:
    return JudgeIdentity(mechanism="model_backend", judge_backend_id="backend")


def _noul(question_id: str = "grounded", **overrides: Any) -> dict[str, Any]:
    return {"id": question_id, "type": "noul", "weight": 1.0, **overrides}


def _choice(question_id: str = "tone", **overrides: Any) -> dict[str, Any]:
    return {
        "id": question_id,
        "type": "choice",
        "options": [{"key": "neutral"}, {"key": "informal"}],
        "passing_keys": ["neutral"],
        "weight": 1.0,
        **overrides,
    }


def _score(question_id: str = "quality", **overrides: Any) -> dict[str, Any]:
    return {
        "id": question_id,
        "type": "score",
        "levels": [{"score": 1}, {"score": 3}, {"score": 5}],
        "pass_from": 3,
        "weight": 1.0,
        **overrides,
    }


def _payload(answers: dict[str, Any]) -> str:
    return json.dumps(answers)


async def _judge(
    backend: _RecordingBackend,
    questions: list[dict[str, Any]],
    *,
    pass_rule: dict[str, Any] | None = None,
    per_question_seconds: float = 2.0,
    per_definition_seconds: float = 5.0,
    max_concurrency: int = transport.MAX_CONCURRENCY,
):
    return await judge_questions(
        _handle(backend),
        questions,
        "the agent output",
        pass_rule=pass_rule or {"kind": "weighted_score_at_least", "threshold": 0.5},
        identity=_identity(),
        per_question_seconds=per_question_seconds,
        per_definition_seconds=per_definition_seconds,
        max_concurrency=max_concurrency,
    )


class TestSchemaDecision:
    async def test_a_native_structured_output_backend_receives_the_schema(self) -> None:
        backend = _RecordingBackend(
            lambda _: _payload({"answer": True, "probabilities": {"yes": 0.9, "no": 0.1}}),
            supports_native_structured_output=True,
        )

        await _judge(backend, [_noul()])

        assert backend.calls[0]["schema"] is not None
        assert backend.calls[0]["schema"]["properties"]["answer"] == {"type": "boolean"}
        assert "ANSWER SPACE" in backend.calls[0]["prompt"]

    async def test_a_non_native_backend_gets_the_schema_embedded_in_the_prompt(self) -> None:
        backend = _RecordingBackend(
            lambda _: _payload({"answer": "neutral", "probabilities": {"neutral": 0.9, "informal": 0.1}}),
            supports_native_structured_output=False,
        )

        await _judge(backend, [_choice()])

        assert backend.calls[0]["schema"] is None
        assert '"neutral"' in backend.calls[0]["prompt"]
        assert "Probabilities must sum to 1" in backend.calls[0]["prompt"]

    async def test_one_backend_call_is_made_per_question(self) -> None:
        answers = {
            "grounded": {"answer": True, "probabilities": {"yes": 0.9, "no": 0.1}},
            "tone": {"answer": "neutral", "probabilities": {"neutral": 0.8, "informal": 0.2}},
            "quality": {"answer": 3, "probabilities": {"1": 0.1, "3": 0.6, "5": 0.3}},
        }
        backend = _RecordingBackend(lambda index: json.dumps(answers[["grounded", "tone", "quality"][index]]))

        verdict = await _judge(backend, [_noul(), _choice(), _score()])

        assert len(backend.calls) == 3
        assert len(verdict.questions) == 3


class TestDerivedPassConfidence:
    async def test_the_verdict_uses_derived_masses_not_the_max_level(self) -> None:
        backend = _RecordingBackend(lambda _: _payload({"answer": 1, "probabilities": {"1": 0.6, "3": 0.3, "5": 0.1}}))

        verdict = await _judge(backend, [_score(pass_from=5, required=True)])

        # Max-level probability is 0.6; the mass AT OR ABOVE pass_from is 0.1.
        assert verdict.questions[0].pass_confidence == pytest.approx(0.1)
        assert verdict.outcome is JudgeOutcome.FAIL

    async def test_choice_mass_is_the_sum_of_passing_keys(self) -> None:
        backend = _RecordingBackend(
            lambda _: _payload({"answer": "neutral", "probabilities": {"neutral": 0.7, "informal": 0.3}})
        )

        verdict = await _judge(backend, [_choice(passing_keys=["neutral", "informal"])])

        assert verdict.questions[0].pass_confidence == pytest.approx(1.0)

    async def test_noul_mass_is_p_yes(self) -> None:
        backend = _RecordingBackend(lambda _: _payload({"answer": True, "probabilities": {"yes": 0.85, "no": 0.15}}))

        verdict = await _judge(backend, [_noul()])

        assert verdict.questions[0].pass_confidence == pytest.approx(0.85)
        assert verdict.confidence == pytest.approx(0.85)
        assert verdict.confidence_kind is JudgeConfidenceKind.MODEL_SELF_REPORTED

    async def test_a_required_question_below_its_minimum_vetoes(self) -> None:
        backend = _RecordingBackend(lambda _: _payload({"answer": True, "probabilities": {"yes": 0.4, "no": 0.6}}))

        verdict = await _judge(backend, [_noul(required=True, min_pass_confidence=0.6)])

        assert verdict.outcome is JudgeOutcome.FAIL

    async def test_a_missing_distribution_falls_back_to_the_pass_indicator(self) -> None:
        backend = _RecordingBackend(lambda _: _payload({"answer": True}))

        verdict = await _judge(backend, [_noul()])

        assert verdict.questions[0].pass_confidence == pytest.approx(1.0)

    async def test_usage_is_captured_as_provenance(self) -> None:
        backend = _RecordingBackend(lambda _: _payload({"answer": True}))

        verdict = await _judge(backend, [_noul()])

        assert verdict.usage is not None
        assert verdict.usage["input_tokens"] == 3
        assert verdict.usage["total_tokens"] == 5
        assert verdict.usage["requests"] == 1


class TestBoundedConcurrency:
    async def test_concurrency_is_bounded_above_one(self) -> None:
        backend = _RecordingBackend(
            lambda _: _payload({"answer": True}),
            delay=0.05,
        )
        questions = [_noul(f"q{index}") for index in range(5)]

        await _judge(backend, questions, max_concurrency=2)

        assert backend.max_in_flight == 2


class TestCorrectiveRetries:
    async def test_a_malformed_answer_is_retried_with_the_rejection(self) -> None:
        backend = _RecordingBackend(lambda index: "not json" if index == 0 else _payload({"answer": True}))

        verdict = await _judge(backend, [_noul()])

        assert len(backend.calls) == 2
        assert "previous response was rejected" in backend.calls[1]["prompt"]
        assert verdict.outcome is JudgeOutcome.PASS

    async def test_a_persistently_malformed_answer_is_a_coded_error(self) -> None:
        backend = _RecordingBackend(lambda _: "not json")

        with pytest.raises(JudgeExecutionError) as exc_info:
            await _judge(backend, [_noul()])

        assert exc_info.value.code is JudgeErrorCode.JUDGE_MALFORMED_RESPONSE
        assert len(backend.calls) == MAX_CORRECTIVE_RETRIES + 1

    async def test_one_broken_question_errors_the_whole_eval(self) -> None:
        def _respond(index: int) -> str:
            if index == 0:
                return json.dumps({"grounded": {"answer": True}})
            return "not json"

        backend = _RecordingBackend(_respond)

        with pytest.raises(JudgeExecutionError):
            await _judge(backend, [_noul(), _choice()])


class TestDeadlinesAndBreaker:
    async def test_a_per_question_deadline_is_a_coded_timeout(self) -> None:
        backend = _RecordingBackend(lambda _: _payload({"answer": True}), delay=1.0)

        with pytest.raises(JudgeExecutionError) as exc_info:
            await _judge(backend, [_noul()], per_question_seconds=0.05)

        assert exc_info.value.code is JudgeErrorCode.JUDGE_TIMEOUT

    async def test_a_per_definition_deadline_bounds_the_batch(self) -> None:
        backend = _RecordingBackend(lambda _: _payload({"answer": True}), delay=1.0)

        with pytest.raises(JudgeExecutionError) as exc_info:
            await _judge(
                backend,
                [_noul("a"), _noul("b")],
                per_question_seconds=0.5,
                per_definition_seconds=0.05,
            )

        assert exc_info.value.code is JudgeErrorCode.JUDGE_TIMEOUT

    async def test_repeated_timeouts_quarantine_the_backend(self) -> None:
        backend = _RecordingBackend(lambda _: _payload({"answer": True}), delay=1.0)
        handle = _handle(backend)

        for _ in range(transport.CIRCUIT_OPEN_AFTER_FAILURES):
            with pytest.raises(JudgeExecutionError):
                await judge_questions(
                    handle,
                    [_noul()],
                    "content",
                    pass_rule={"kind": "weighted_score_at_least", "threshold": 0.5},
                    identity=_identity(),
                    per_question_seconds=0.01,
                    per_definition_seconds=0.5,
                )

        calls_before = len(backend.calls)
        with pytest.raises(JudgeExecutionError) as exc_info:
            await judge_questions(
                handle,
                [_noul()],
                "content",
                pass_rule={"kind": "weighted_score_at_least", "threshold": 0.5},
                identity=_identity(),
                per_question_seconds=0.01,
                per_definition_seconds=0.5,
            )

        assert exc_info.value.code is JudgeErrorCode.JUDGE_UNAVAILABLE
        assert "quarantined" in exc_info.value.detail
        assert len(backend.calls) == calls_before


class _HangingAsyncBackend:
    """Answers the first call by hanging forever; later calls succeed."""

    def __init__(self) -> None:
        self.calls = 0

    async def invoke(self, messages: list[Any], **_: Any) -> AIMessage:
        self.calls += 1
        if self.calls == 1:
            await asyncio.sleep(3600)
        return AIMessage(content=json.dumps({"answer": True, "probabilities": {"yes": 1.0, "no": 0.0}}))


async def test_a_hung_async_judge_is_cancelled_and_the_next_call_succeeds() -> None:
    backend = _HangingAsyncBackend()
    handle = JudgeBackendHandle(
        key="hung-backend",
        invoke=lambda messages, schema: backend.invoke(messages),
    )

    with pytest.raises(JudgeExecutionError) as exc_info:
        await judge_questions(
            handle,
            [_noul()],
            "content",
            pass_rule={"kind": "weighted_score_at_least", "threshold": 0.5},
            identity=_identity(),
            per_question_seconds=0.05,
            per_definition_seconds=1.0,
        )

    second = await judge_questions(
        handle,
        [_noul()],
        "content",
        pass_rule={"kind": "weighted_score_at_least", "threshold": 0.5},
        identity=_identity(),
        per_question_seconds=1.0,
        per_definition_seconds=2.0,
    )

    assert exc_info.value.code is JudgeErrorCode.JUDGE_TIMEOUT
    assert second.outcome is JudgeOutcome.PASS


# ---------------------------------------------------------------------------
# The sync bridge: an UNCANCELLABLE call must not wedge later calls (D10)
# ---------------------------------------------------------------------------


class _BlockingBackend:
    """Blocks the event loop thread itself — cancellation cannot interrupt it."""

    def __init__(self, *, blocked: bool) -> None:
        self.blocked = blocked

    async def invoke(self, messages: list[Any], **_: Any) -> AIMessage:
        if self.blocked:
            # Block the loop thread itself (not awaitable, so cancellation
            # cannot interrupt it). threading.Event.wait is deliberate here:
            # an asyncio.sleep would be cancellable and would not reproduce the
            # uncancellable wedge this test exists to prove.
            threading.Event().wait(0.4)
        return AIMessage(content=json.dumps({"answer": True, "probabilities": {"yes": 0.9, "no": 0.1}}))


class _FakeHub:
    def __init__(self, backend: Any) -> None:
        self._backend = backend

    async def get(self, backend_id: uuid.UUID, **_: Any) -> Any:
        return self._backend


def _typed_eval_def() -> EvalDefinition:
    return EvalDefinition(
        id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        name="typed-judge",
        eval_type=EvalType.MODEL_JUDGE,
        config={
            "answer_contract": "typed_questions",
            "field": "output",
            "backing": {"kind": "model_backend", "model_backend_id": str(uuid.uuid4())},
            "questions": [{"id": "grounded", "type": "noul", "weight": 1.0}],
            "pass_rule": {"kind": "weighted_score_at_least", "threshold": 0.5},
            "timeouts": {"per_question_seconds": 5, "per_definition_seconds": 10},
        },
        failure_behaviour="warn",
    )


def test_an_uncancellable_judge_cannot_wedge_later_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """The stuck loop is RETIRED, so the next judge call gets a fresh loop.

    The blocking backend holds the loop thread inside ``time.sleep``, so the
    loop-side cancellation cannot interrupt it. Without retire-on-wedge the
    persistent loop would stay blocked and every later call would fail closed
    until a process restart.
    """
    monkeypatch.setattr(llm_judge, "BACKEND_INVOKE_TIMEOUT_SECONDS", 0.1)
    monkeypatch.setattr(llm_judge, "SYNC_BRIDGE_TIMEOUT_SECONDS", 0.2)
    eval_def = _typed_eval_def()
    blocked = llm_judge.build_model_judge_callable(_FakeHub(_BlockingBackend(blocked=True)), str(uuid.uuid4()))

    with pytest.raises(JudgeExecutionError) as exc_info:
        blocked({"output": "agent output"}, eval_def)

    healthy = llm_judge.build_model_judge_callable(_FakeHub(_BlockingBackend(blocked=False)), str(uuid.uuid4()))
    second = healthy({"output": "agent output"}, eval_def)

    assert exc_info.value.code is JudgeErrorCode.JUDGE_TIMEOUT
    assert second["outcome"] == "pass"


def test_the_async_judge_runs_without_the_bridge(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both high-volume call sites use ``ajudge`` — never ``run_coroutine_sync``."""
    eval_def = _typed_eval_def()
    judge = llm_judge.build_model_judge_callable(_FakeHub(_BlockingBackend(blocked=False)), str(uuid.uuid4()))
    monkeypatch.setattr(
        llm_judge,
        "run_coroutine_sync",
        lambda *_: pytest.fail("the async judge path must not use the sync bridge"),
    )

    result = asyncio.run(judge.ajudge({"output": "agent output"}, eval_def))

    assert result["outcome"] == "pass"
