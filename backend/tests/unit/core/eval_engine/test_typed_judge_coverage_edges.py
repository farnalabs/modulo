"""Coverage edges for the typed judge (ADR 066 S2).

Each case pins a documented failure branch the primary suites do not reach:
malformed probability payloads, the async judge's error ladder, the transport's
schema/response rejections, the circuit breaker's expiry, the bridge's
retire/stop paths, and the suite runner's async branch.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import threading
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.messages import AIMessage

import modulo.core.pipeline_engine.llm_judge as llm_judge
from modulo.core.eval_engine import EvalDefinition, EvalEngine, EvalType
from modulo.core.eval_engine.eval_definition_write import _resolve_typed_backings
from modulo.core.eval_engine.execute_suite_run import _case_results_async, _run_suite_cases
from modulo.core.eval_engine.typed_judge import (
    JudgeConfigWriteError,
    JudgeErrorCode,
    JudgeExecutionError,
    JudgeIdentity,
    JudgeOutcome,
    QuestionVerdict,
    aggregate_typed_answers,
    build_question_prompt,
    derived_pass_confidence,
    transport,
)
from modulo.core.eval_engine.typed_judge import bridge as judge_bridge
from modulo.core.eval_engine.typed_judge.config import typed_config_questions
from modulo.core.eval_engine.typed_judge.transport import JudgeBackendHandle, judge_questions, reset_circuit_breaker


@pytest.fixture(autouse=True)
def _fresh_state():
    reset_circuit_breaker()
    judge_bridge.reset_bridge()
    yield
    reset_circuit_breaker()
    judge_bridge.reset_bridge()


def _pass_rule() -> dict[str, Any]:
    return {"kind": "weighted_score_at_least", "threshold": 0.5}


def _identity() -> JudgeIdentity:
    return JudgeIdentity(mechanism="model_backend", judge_backend_id="edge-backend")


def _handle(responder: Any, *, native: bool = False, key: str = "edge-backend") -> JudgeBackendHandle:
    async def _invoke(messages: list[Any], output_schema: dict[str, Any] | None) -> Any:
        return responder(messages, output_schema)

    return JudgeBackendHandle(key=key, invoke=_invoke, supports_native_structured_output=native)


def _noul(question_id: str = "q", **overrides: Any) -> dict[str, Any]:
    return {"id": question_id, "type": "noul", "weight": 1.0, **overrides}


class TestProbabilityEdges:
    def _aggregate(self, question: dict[str, Any], payload: Any) -> Any:
        return aggregate_typed_answers([question], {"q": payload}, _pass_rule())

    @pytest.mark.parametrize("probabilities", [[], {}, "high"])
    def test_a_non_object_or_empty_distribution_is_malformed(self, probabilities: Any) -> None:
        with pytest.raises(JudgeExecutionError) as exc_info:
            self._aggregate(_noul(), {"answer": True, "probabilities": probabilities})

        assert exc_info.value.code is JudgeErrorCode.JUDGE_MALFORMED_RESPONSE

    def test_a_negative_probability_is_malformed(self) -> None:
        with pytest.raises(JudgeExecutionError) as exc_info:
            self._aggregate(_noul(), {"answer": True, "probabilities": {"yes": -0.1, "no": 1.0}})

        assert exc_info.value.code is JudgeErrorCode.JUDGE_MALFORMED_RESPONSE

    def test_a_zero_mass_distribution_is_malformed(self) -> None:
        with pytest.raises(JudgeExecutionError) as exc_info:
            self._aggregate(_noul(), {"answer": True, "probabilities": {"yes": 0.0, "no": 0.0}})

        assert exc_info.value.code is JudgeErrorCode.JUDGE_MALFORMED_RESPONSE

    def test_an_unknown_probability_key_is_malformed(self) -> None:
        with pytest.raises(JudgeExecutionError) as exc_info:
            self._aggregate(_noul(), {"answer": True, "probabilities": {"maybe": 1.0}})

        assert exc_info.value.code is JudgeErrorCode.JUDGE_MALFORMED_RESPONSE

    def test_a_non_numeric_score_probability_key_is_malformed(self) -> None:
        question = {
            "id": "q",
            "type": "score",
            "levels": [{"score": 1}, {"score": 3}],
            "pass_from": 3,
            "weight": 1.0,
        }

        with pytest.raises(JudgeExecutionError) as exc_info:
            self._aggregate(question, {"answer": 3, "probabilities": {"high": 1.0}})

        assert exc_info.value.code is JudgeErrorCode.JUDGE_MALFORMED_RESPONSE

    def test_a_non_dict_level_is_skipped_in_the_probability_path(self) -> None:
        question = {
            "id": "q",
            "type": "score",
            "levels": [{"score": 1}, "junk", {"score": 3}],
            "pass_from": 3,
            "weight": 1.0,
        }

        verdicts, _, _ = self._aggregate(question, {"answer": 3, "probabilities": {"1": 0.4, "3": 0.6}})

        assert verdicts[0].pass_confidence == pytest.approx(0.6)

    def test_an_invalid_min_pass_confidence_is_configuration(self) -> None:
        with pytest.raises(JudgeExecutionError) as exc_info:
            self._aggregate(
                _noul(required=True, min_pass_confidence=2.0),
                {"answer": True, "probabilities": {"yes": 1.0, "no": 0.0}},
            )

        assert exc_info.value.code is JudgeErrorCode.JUDGE_CONFIGURATION


class TestDerivedConfidenceEdges:
    def test_a_question_without_a_verdict_is_ignored(self) -> None:
        question = _noul()
        verdicts, _, _ = aggregate_typed_answers([question], {"q": True}, _pass_rule())

        assert derived_pass_confidence(verdicts, [question, {"id": "other", "weight": 1.0}]) == pytest.approx(1.0)

    def test_a_zero_or_boolean_weight_is_excluded_from_the_mean(self) -> None:
        question = _noul()
        verdicts, _, _ = aggregate_typed_answers([question], {"q": True}, _pass_rule())

        assert derived_pass_confidence(verdicts, [{**question, "weight": 0}]) is None
        assert derived_pass_confidence(verdicts, [{**question, "weight": True}]) is None

    def test_a_verdict_without_pass_confidence_is_skipped(self) -> None:
        verdicts = [QuestionVerdict(question_id="q")]

        assert derived_pass_confidence(verdicts, [_noul()]) is None


class TestPromptShapeEdges:
    def test_a_choice_without_options_renders_an_empty_answer_space(self) -> None:
        assert "ANSWER SPACE: []" in build_question_prompt({"id": "q", "type": "choice"}, "output")

    def test_a_score_without_levels_renders_an_empty_answer_space(self) -> None:
        assert "ANSWER SPACE: []" in build_question_prompt({"id": "q", "type": "score"}, "output")

    def test_an_unknown_question_type_renders_an_empty_answer_space(self) -> None:
        assert "ANSWER SPACE: []" in build_question_prompt({"id": "q", "type": "essay"}, "output")


class TestTransportRejections:
    async def _judge(self, handle: JudgeBackendHandle, question: dict[str, Any]) -> Any:
        return await judge_questions(
            handle,
            [question],
            "content",
            pass_rule=_pass_rule(),
            identity=_identity(),
            per_question_seconds=0.5,
            per_definition_seconds=2.0,
        )

    async def test_an_unsupported_question_type_is_configuration(self) -> None:
        handle = _handle(lambda messages, schema: AIMessage(content="{}"), native=True)

        with pytest.raises(JudgeExecutionError) as exc_info:
            await self._judge(handle, {"id": "q", "type": "essay"})

        assert exc_info.value.code is JudgeErrorCode.JUDGE_CONFIGURATION

    async def test_a_response_missing_the_answers_map_is_malformed(self) -> None:
        handle = _handle(lambda messages, schema: AIMessage(content=json.dumps({"answers": {}})))

        with pytest.raises(JudgeExecutionError) as exc_info:
            await self._judge(handle, _noul())

        assert exc_info.value.code is JudgeErrorCode.JUDGE_MALFORMED_RESPONSE

    async def test_a_non_object_response_is_malformed(self) -> None:
        handle = _handle(lambda messages, schema: AIMessage(content="[]"))

        with pytest.raises(JudgeExecutionError) as exc_info:
            await self._judge(handle, _noul())

        assert exc_info.value.code is JudgeErrorCode.JUDGE_MALFORMED_RESPONSE

    async def test_a_null_answer_is_malformed(self) -> None:
        handle = _handle(lambda messages, schema: AIMessage(content=json.dumps({"answer": None})))

        with pytest.raises(JudgeExecutionError) as exc_info:
            await self._judge(handle, _noul())

        assert exc_info.value.code is JudgeErrorCode.JUDGE_MALFORMED_RESPONSE

    async def test_a_non_finite_json_constant_is_malformed(self) -> None:
        handle = _handle(lambda messages, schema: AIMessage(content='{"answer": NaN}'))

        with pytest.raises(JudgeExecutionError) as exc_info:
            await self._judge(handle, _noul())

        assert exc_info.value.code is JudgeErrorCode.JUDGE_MALFORMED_RESPONSE

    async def test_a_question_without_an_id_uses_the_placeholder_label(self) -> None:
        handle = _handle(lambda messages, schema: AIMessage(content="[]"))

        with pytest.raises(JudgeExecutionError) as exc_info:
            await self._judge(handle, {"type": "noul"})

        assert "'?'" in exc_info.value.detail

    async def test_usage_from_response_metadata_is_captured(self) -> None:
        def _responder(messages: list[Any], schema: Any) -> AIMessage:
            return AIMessage(
                content=json.dumps({"answer": True}),
                response_metadata={"token_usage": {"prompt_tokens": 4, "completion_tokens": 1}},
            )

        verdict = await self._judge(_handle(_responder), _noul())

        assert verdict.usage is not None
        assert verdict.usage["prompt_tokens"] == 4

    async def test_an_absent_usage_figure_is_not_fabricated(self) -> None:
        handle = _handle(lambda messages, schema: AIMessage(content=json.dumps({"answer": True})))

        verdict = await self._judge(handle, _noul())

        assert verdict.usage is None

    async def test_no_questions_is_a_configuration_error(self) -> None:
        with pytest.raises(JudgeExecutionError) as exc_info:
            await judge_questions(
                _handle(lambda messages, schema: AIMessage(content="{}")),
                [],
                "content",
                pass_rule=_pass_rule(),
                identity=_identity(),
                per_question_seconds=0.5,
                per_definition_seconds=2.0,
            )

        assert exc_info.value.code is JudgeErrorCode.JUDGE_CONFIGURATION

    async def test_native_choice_and_score_schemas_enumerate_the_answer_space(self) -> None:
        seen: list[Any] = []

        def _responder(messages: list[Any], schema: Any) -> AIMessage:
            seen.append(schema)
            answer: Any = 3 if schema["properties"]["answer"].get("type") == "number" else "neutral"
            return AIMessage(content=json.dumps({"answer": answer}))

        choice = {
            "id": "tone",
            "type": "choice",
            "options": [{"key": "neutral"}, {"key": "informal"}],
            "passing_keys": ["neutral"],
            "weight": 1.0,
        }
        await self._judge(_handle(_responder, native=True), choice)

        score = {
            "id": "quality",
            "type": "score",
            "levels": [{"score": 1}, {"score": 3}],
            "pass_from": 3,
            "weight": 1.0,
        }
        await self._judge(_handle(_responder, native=True), score)

        assert seen[0]["properties"]["answer"]["enum"] == ["neutral", "informal"]
        assert set(seen[0]["properties"]["probabilities"]["properties"].keys()) == {"neutral", "informal"}
        assert seen[1]["properties"]["answer"]["enum"] == [1.0, 3.0]

    async def test_a_payload_level_detail_is_preserved(self) -> None:
        handle = _handle(
            lambda messages, schema: AIMessage(content=json.dumps({"answer": True, "detail": "payload reason"}))
        )

        verdict = await self._judge(handle, _noul())

        assert verdict.detail == "payload reason"

    async def test_an_expired_quarantine_is_retried(self) -> None:
        transport._BREAKER._blocked_until[_identity().judge_backend_id or ""] = 0.0
        handle = _handle(lambda messages, schema: AIMessage(content=json.dumps({"answer": True})))

        verdict = await self._judge(handle, _noul())

        assert verdict.outcome is JudgeOutcome.PASS


class TestLlmJudgeAsyncEdges:
    def _eval_def(self, **config: Any) -> EvalDefinition:
        return EvalDefinition(
            id=uuid.uuid4(),
            org_id=uuid.uuid4(),
            name="typed",
            eval_type=EvalType.MODEL_JUDGE,
            config={
                "answer_contract": "typed_questions",
                "field": "output",
                "backing": {"kind": "model_backend", "model_backend_id": str(uuid.uuid4())},
                "questions": [{"id": "q", "type": "noul", "weight": 1.0}],
                "pass_rule": _pass_rule(),
                **config,
            },
            failure_behaviour="warn",
        )

    async def test_a_hung_hub_resolve_is_a_coded_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _SlowHub:
            async def get(self, backend_id: uuid.UUID, **_: Any) -> Any:
                await asyncio.sleep(1)

        monkeypatch.setattr(llm_judge, "BACKEND_INVOKE_TIMEOUT_SECONDS", 0.01)
        judge = llm_judge.build_model_judge_callable(_SlowHub(), str(uuid.uuid4()))

        with pytest.raises(JudgeExecutionError) as exc_info:
            await judge.ajudge({"output": "x"}, self._eval_def())

        assert exc_info.value.code is JudgeErrorCode.JUDGE_TIMEOUT

    async def test_a_transport_timeout_is_re_raised(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def _timeout(*args: Any, **kwargs: Any) -> Any:
            raise TimeoutError("slow")

        class _Hub:
            async def get(self, backend_id: uuid.UUID, **_: Any) -> Any:
                return SimpleNamespace(invoke=lambda messages, **kw: None)

        monkeypatch.setattr(llm_judge, "judge_questions", _timeout)
        judge = llm_judge.build_model_judge_callable(_Hub(), str(uuid.uuid4()))

        with pytest.raises(JudgeExecutionError) as exc_info:
            await judge.ajudge({"output": "x"}, self._eval_def())

        assert exc_info.value.code is JudgeErrorCode.JUDGE_TIMEOUT

    async def test_a_generic_transport_failure_is_judge_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def _boom(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("provider exploded")

        class _Hub:
            async def get(self, backend_id: uuid.UUID, **_: Any) -> Any:
                return SimpleNamespace(invoke=lambda messages, **kw: None)

        monkeypatch.setattr(llm_judge, "judge_questions", _boom)
        judge = llm_judge.build_model_judge_callable(_Hub(), str(uuid.uuid4()))

        with pytest.raises(JudgeExecutionError) as exc_info:
            await judge.ajudge({"output": "x"}, self._eval_def())

        assert exc_info.value.code is JudgeErrorCode.JUDGE_UNAVAILABLE

    async def test_a_cancellation_from_the_async_judge_is_never_swallowed(self) -> None:
        class _Backend:
            async def invoke(self, messages: list[Any], **_: Any) -> Any:
                raise asyncio.CancelledError

        class _Hub:
            async def get(self, backend_id: uuid.UUID, **_: Any) -> Any:
                return _Backend()

        judge = llm_judge.build_model_judge_callable(_Hub(), str(uuid.uuid4()))

        with pytest.raises(asyncio.CancelledError):
            await judge.ajudge({"output": "x"}, self._eval_def())

    async def test_a_native_backend_receives_the_output_schema(self) -> None:
        seen: dict[str, Any] = {}

        class _Backend:
            supports_native_structured_output = True

            async def invoke(self, messages: list[Any], **kwargs: Any) -> Any:
                seen["kwargs"] = kwargs
                return AIMessage(content=json.dumps({"answer": True}))

        class _Hub:
            async def get(self, backend_id: uuid.UUID, **_: Any) -> Any:
                return _Backend()

        judge = llm_judge.build_model_judge_callable(_Hub(), str(uuid.uuid4()))
        verdict = await judge.ajudge({"output": "x"}, self._eval_def())

        assert verdict["outcome"] == "pass"
        assert seen["kwargs"]["output_schema"] is not None

    async def test_a_judge_execution_error_is_re_raised(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def _coded(*args: Any, **kwargs: Any) -> Any:
            raise JudgeExecutionError(JudgeErrorCode.JUDGE_MALFORMED_RESPONSE, "malformed")

        class _Hub:
            async def get(self, backend_id: uuid.UUID, **_: Any) -> Any:
                return SimpleNamespace(invoke=lambda messages, **kw: None)

        monkeypatch.setattr(llm_judge, "judge_questions", _coded)
        judge = llm_judge.build_model_judge_callable(_Hub(), str(uuid.uuid4()))

        with pytest.raises(JudgeExecutionError) as exc_info:
            await judge.ajudge({"output": "x"}, self._eval_def())

        assert exc_info.value.code is JudgeErrorCode.JUDGE_MALFORMED_RESPONSE

    async def test_a_non_native_backend_uses_the_embedded_prompt(self) -> None:
        seen: dict[str, Any] = {}

        class _Backend:
            supports_native_structured_output = False

            async def invoke(self, messages: list[Any], **kwargs: Any) -> Any:
                seen["kwargs"] = kwargs
                return AIMessage(content=json.dumps({"answer": True}))

        class _Hub:
            async def get(self, backend_id: uuid.UUID, **_: Any) -> Any:
                return _Backend()

        judge = llm_judge.build_model_judge_callable(_Hub(), str(uuid.uuid4()))
        verdict = await judge.ajudge({"output": "x"}, self._eval_def())

        assert verdict["outcome"] == "pass"
        assert "output_schema" not in seen["kwargs"]

    def test_an_unresolvable_dotted_field_is_configuration(self) -> None:
        eval_def = self._eval_def(field="missing.path")

        with pytest.raises(JudgeExecutionError) as exc_info:
            llm_judge._judge_content({"output": "x"}, eval_def.config)

        assert exc_info.value.code is JudgeErrorCode.JUDGE_CONFIGURATION

    def test_a_generic_failure_outside_the_judge_runner_is_judge_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("unexpected")

        monkeypatch.setattr(llm_judge, "_run_judge", _boom)
        judge = llm_judge.build_model_judge_callable(object(), str(uuid.uuid4()))

        with pytest.raises(JudgeExecutionError) as exc_info:
            judge({"output": "x"}, self._eval_def())

        assert exc_info.value.code is JudgeErrorCode.JUDGE_UNAVAILABLE

    def test_typed_config_questions_tolerates_a_legacy_or_broken_list(self) -> None:
        assert not typed_config_questions({"answer_contract": "typed_questions", "questions": "x"})
        assert not typed_config_questions({"field": "output"})
        assert typed_config_questions({"answer_contract": "typed_questions", "questions": [{"id": "q"}, "junk"]}) == [
            {"id": "q"}
        ]


class TestEngineAsyncErrors:
    async def _result(self, judge: Any, **config: Any) -> Any:
        eval_def = EvalDefinition(
            id=uuid.uuid4(),
            org_id=uuid.uuid4(),
            name="typed",
            eval_type=EvalType.MODEL_JUDGE,
            config={
                "answer_contract": "typed_questions",
                "field": "output",
                "questions": [{"id": "q", "type": "noul", "weight": 1.0}],
                "pass_rule": _pass_rule(),
                **config,
            },
            failure_behaviour="warn",
        )
        return await EvalEngine().evaluate_result_async({"output": "x"}, eval_def, model_judge_callable=judge)

    async def test_an_absent_async_judge_is_an_error(self) -> None:
        result = await self._result(None)

        assert result.outcome is JudgeOutcome.ERROR
        assert result.judge_verdict is not None
        assert result.judge_verdict.error_code is JudgeErrorCode.JUDGE_UNAVAILABLE

    async def test_a_generic_async_judge_failure_is_an_error(self) -> None:
        class _Boom:
            async def ajudge(self, output: dict[str, Any], eval_def: Any) -> Any:
                raise RuntimeError("boom")

        result = await self._result(_Boom())

        assert result.outcome is JudgeOutcome.ERROR
        assert result.judge_verdict is not None
        assert result.judge_verdict.error_code is JudgeErrorCode.JUDGE_UNAVAILABLE

    async def test_a_cancellation_from_an_async_judge_propagates(self) -> None:
        class _Interrupt:
            async def ajudge(self, output: dict[str, Any], eval_def: Any) -> Any:
                raise asyncio.CancelledError

        with pytest.raises(asyncio.CancelledError):
            await self._result(_Interrupt())

    async def test_an_over_bound_payload_is_an_async_error(self) -> None:
        eval_def = EvalDefinition(
            id=uuid.uuid4(),
            org_id=uuid.uuid4(),
            name="typed",
            eval_type=EvalType.MODEL_JUDGE,
            config={
                "answer_contract": "typed_questions",
                "field": "output",
                "context_fields": ["scenario.payload"],
                "questions": [{"id": "q", "type": "noul", "weight": 1.0}],
                "pass_rule": _pass_rule(),
            },
            failure_behaviour="warn",
        )

        result = await EvalEngine().evaluate_result_async(
            {"output": "x", "scenario": {"payload": "y" * 100_001}},
            eval_def,
            model_judge_callable=object(),
        )

        assert result.outcome is JudgeOutcome.ERROR
        assert result.judge_verdict is not None
        assert result.judge_verdict.error_code is JudgeErrorCode.JUDGE_CONFIGURATION

    async def test_an_unresolvable_payload_is_an_async_error(self) -> None:
        result = await self._result(object(), context_fields=["scenario.missing"])

        assert result.outcome is JudgeOutcome.ERROR
        assert result.judge_verdict is not None
        assert result.judge_verdict.error_code is JudgeErrorCode.JUDGE_CONFIGURATION


class TestWriteResolutionEdges:
    async def test_a_non_uuid_backend_id_is_a_typed_config_error(self) -> None:
        with pytest.raises(JudgeConfigWriteError) as exc_info:
            await _resolve_typed_backings(
                object(),  # type: ignore[arg-type]
                org_id=uuid.uuid4(),
                config_json={
                    "answer_contract": "typed_questions",
                    "backing": {"kind": "model_backend", "model_backend_id": "not-a-uuid"},
                },
            )

        assert exc_info.value.code == "judge_config_invalid"


class TestSuiteAsyncEdges:
    async def test_an_async_engine_failure_still_produces_an_error_row(self) -> None:
        class _BoomEngine:
            async def evaluate_result_async(self, *args: Any, **kwargs: Any) -> Any:
                raise RuntimeError("engine exploded")

        run = SimpleNamespace(id=uuid.uuid4(), organisation_id=uuid.uuid4())
        case = SimpleNamespace(id=uuid.uuid4(), input_payload={"content": "x"})
        definition = EvalDefinition(
            id=uuid.uuid4(),
            org_id=run.organisation_id,
            name="judge",
            eval_type=EvalType.MODEL_JUDGE,
            config={"field": "output", "questions": [{"id": "q", "type": "noul"}]},
            failure_behaviour="warn",
        )

        results, errored = await _case_results_async(
            run,  # type: ignore[arg-type]
            case,  # type: ignore[arg-type]
            [definition],
            _BoomEngine(),  # type: ignore[arg-type]
            None,
            None,
            eval_definition_version=1,
        )

        assert errored == 1
        assert results[0].outcome == JudgeOutcome.ERROR.value

    async def test_the_suite_loop_uses_the_async_path_for_an_async_judge(self) -> None:
        class _AsyncJudge:
            async def ajudge(self, output: dict[str, Any], eval_def: Any) -> Any:
                return {"outcome": "pass", "score": 1.0}

        session: Any = SimpleNamespace(add=lambda row: None)
        run = SimpleNamespace(id=uuid.uuid4(), organisation_id=uuid.uuid4(), claimed_cost=None)
        case = SimpleNamespace(id=uuid.uuid4(), input_payload={"content": "x"})
        definition = EvalDefinition(
            id=uuid.uuid4(),
            org_id=run.organisation_id,
            name="judge",
            eval_type=EvalType.MODEL_JUDGE,
            config={"field": "output", "questions": [{"id": "q", "type": "noul"}]},
            failure_behaviour="warn",
        )

        passed, failed, excluded = await _run_suite_cases(
            session,
            run,  # type: ignore[arg-type]
            cases=[case],  # type: ignore[list-item]
            suite_defs=[definition],
            engine=EvalEngine(),
            model_judge_callable=_AsyncJudge(),
            scenario_inputs=None,
            eval_definition_version=1,
            claims_llm=False,
            suite_ceiling=None,
            cost_per_llm_case=0,  # type: ignore[arg-type]
        )

        assert (passed, failed, excluded) == (1, 0, 0)


class TestBridgeEdges:
    def test_cancel_or_retire_with_no_started_task_retires_the_loop(self) -> None:
        loop = judge_bridge._LoopThread()
        try:
            loop._cancel_or_retire({}, threading.Event())

            assert loop.retired is True
        finally:
            loop.stop_when_idle()

    def test_stop_when_idle_cancels_a_pending_task(self) -> None:
        loop = judge_bridge._LoopThread()
        started = threading.Event()

        async def _sleeper() -> None:
            started.set()
            await asyncio.sleep(30)

        future = asyncio.run_coroutine_threadsafe(_sleeper(), loop.loop)
        assert started.wait(2)
        loop.stop_when_idle()

        with pytest.raises(concurrent.futures.CancelledError):
            future.result(timeout=3)
