"""Changed-lines coverage for the FAR-2040 judge execution surfaces.

Focused branch coverage for the engine/dispatcher/mcp edges this feature PR
introduced or moved. Each case exercises one documented failure ladder; the
contract itself is pinned by the feature's own suites.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.messages import AIMessage
from starlette.exceptions import HTTPException as StarletteHTTPException

import modulo.core.pipeline_engine.llm_judge as llm_judge
import modulo.core.pipeline_engine.model_backend_errors as model_backend_errors
from modulo.api.mcp_server import _tool_exception_error
from modulo.core.eval_engine import (
    EvalDefinition,
    EvalEngine,
    EvalResult,
    EvalType,
    _result_from_judge_dict,
)
from modulo.core.eval_engine.execute_suite_run import _case_results
from modulo.core.eval_engine.suite_run_judge import build_suite_run_model_judge_callable
from modulo.core.eval_engine.typed_judge import (
    JudgeErrorCode,
    JudgeExecutionError,
    JudgeIdentity,
    JudgeOutcome,
    JudgeVerdict,
)
from modulo.core.guardrails.correction import (
    CorrectionDefinition,
    CorrectionDetectorFamily,
    _run_different_family_revalidation,
)
from modulo.core.model_backend_hub import BackendNotFoundError


class _FakeBackend:
    def __init__(self, content: str) -> None:
        self._content = content

    async def invoke(self, messages: list[Any], **kwargs: Any) -> AIMessage:
        return AIMessage(content=self._content)


class _FakeHub:
    def __init__(self, content: str) -> None:
        self._backend = _FakeBackend(content)

    async def get(self, backend_id: uuid.UUID, **kwargs: Any) -> _FakeBackend:
        return self._backend


def _eval_def(config: dict[str, Any] | None = None) -> EvalDefinition:
    return EvalDefinition(
        id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        node_id="n1",
        name="judge",
        eval_type=EvalType.MODEL_JUDGE,
        config={"field": "output", **(config or {})},
        failure_behaviour="warn",
    )


class TestEvalEngineEdges:
    def test_a_non_dict_model_input_passes_through_the_before_validator(self) -> None:
        assert EvalResult._project_passed_from_outcome("not-a-dict") == "not-a-dict"

    def test_an_explicit_judge_verdict_overrides_the_payload_identity(self) -> None:
        verdict = JudgeVerdict(
            outcome=JudgeOutcome.PASS,
            judge=JudgeIdentity(model="explicit"),
        )

        result = _result_from_judge_dict(
            {"passed": True, "score": 1.0, "judge": {"model": "payload"}},
            uuid.uuid4(),
            "n1",
            uuid.uuid4(),
            judge_verdict=verdict,
        )

        assert result.judge_verdict is not None
        assert result.judge_verdict.judge.model == "explicit"

    def test_a_cancellation_is_re_raised(self) -> None:
        def _cancel() -> dict[str, Any]:
            raise KeyboardInterrupt

        with pytest.raises(KeyboardInterrupt):
            EvalEngine._run_model_judge_and_parse(
                _cancel,
                (),
                eval_def=_eval_def(),
                run_id=uuid.uuid4(),
            )

    def test_a_malformed_response_becomes_an_error_result(self) -> None:
        result = EvalEngine._run_model_judge_and_parse(
            lambda: "not-a-dict",
            (),
            eval_def=_eval_def(),
            run_id=uuid.uuid4(),
        )

        assert result.outcome is JudgeOutcome.ERROR
        assert result.score is None
        assert result.judge_verdict is not None

    def test_custom_eval_without_a_field_and_without_whole_output_raises(self) -> None:
        eval_def = EvalDefinition(
            id=uuid.uuid4(),
            org_id=uuid.uuid4(),
            node_id="n1",
            name="custom",
            eval_type=EvalType.MODEL_JUDGE,
            config={},
            failure_behaviour="warn",
        )

        with pytest.raises(JudgeExecutionError) as exc_info:
            EvalEngine._build_safe_judge_input({}, eval_def)

        assert exc_info.value.code is JudgeErrorCode.JUDGE_CONFIGURATION

    def test_error_result_is_a_public_no_verdict_constructor(self) -> None:
        eval_def = _eval_def()

        result = EvalEngine.error_result(
            eval_def,
            uuid.uuid4(),
            JudgeErrorCode.JUDGE_UNAVAILABLE,
            "no verdict",
        )

        assert result.outcome is JudgeOutcome.ERROR
        assert result.score is None
        assert result.eval_id == eval_def.id


class TestCaseResultsEdges:
    def test_an_unexpected_engine_failure_still_produces_an_error_row(self) -> None:
        definition = _eval_def()

        class _BoomEngine:
            def evaluate_result(self, *args: Any, **kwargs: Any) -> EvalResult:
                raise RuntimeError("engine exploded")

        results, errored = _case_results(
            SimpleNamespace(id=uuid.uuid4(), organisation_id=uuid.uuid4()),
            SimpleNamespace(id=uuid.uuid4(), input_payload={"x": 1}),
            [definition],
            _BoomEngine(),  # type: ignore[arg-type]
            None,
            None,
            eval_definition_version=1,
        )

        assert len(results) == 1
        assert results[0].outcome == JudgeOutcome.ERROR.value
        assert errored == 1


class TestSuiteRunJudgeEdges:
    def test_an_unknown_backend_is_a_configuration_error(self, monkeypatch) -> None:
        backend_id = uuid.uuid4()

        def _boom(output: dict[str, Any], eval_def: EvalDefinition) -> dict[str, Any]:
            raise BackendNotFoundError(backend_id)

        monkeypatch.setattr(llm_judge, "build_model_judge_callable", lambda hub, key: _boom)
        judge = build_suite_run_model_judge_callable(object())  # type: ignore[arg-type]

        with pytest.raises(JudgeExecutionError) as exc_info:
            judge({"output": "x"}, _eval_def({"model_backend_id": str(backend_id)}))

        assert exc_info.value.code is JudgeErrorCode.JUDGE_CONFIGURATION


class TestLlmJudgeEdges:
    def test_a_non_finite_json_constant_is_malformed(self) -> None:
        judge = llm_judge.build_model_judge_callable(
            _FakeHub('{"passed": true, "score": NaN}'),
            str(uuid.uuid4()),
        )

        with pytest.raises(JudgeExecutionError) as exc_info:
            judge({"output": "x"}, _eval_def())

        assert exc_info.value.code is JudgeErrorCode.JUDGE_MALFORMED_RESPONSE

    def test_a_non_object_json_response_is_malformed(self) -> None:
        judge = llm_judge.build_model_judge_callable(_FakeHub("[1, 2]"), str(uuid.uuid4()))

        with pytest.raises(JudgeExecutionError) as exc_info:
            judge({"output": "x"}, _eval_def())

        assert exc_info.value.code is JudgeErrorCode.JUDGE_MALFORMED_RESPONSE

    def test_classify_backend_not_found_is_configuration(self) -> None:
        code = llm_judge._classify_provider_exception(BackendNotFoundError(uuid.uuid4()))

        assert code is JudgeErrorCode.JUDGE_CONFIGURATION

    @pytest.mark.parametrize(
        ("provider_code", "expected"),
        [
            ("provider.authentication", JudgeErrorCode.PROVIDER_AUTHENTICATION),
            ("provider.billing", JudgeErrorCode.PROVIDER_BILLING),
            ("provider.rate_limit", JudgeErrorCode.JUDGE_UNAVAILABLE),
            (None, JudgeErrorCode.JUDGE_UNAVAILABLE),
        ],
    )
    def test_classify_provider_codes(self, monkeypatch, provider_code: str | None, expected) -> None:
        monkeypatch.setattr(model_backend_errors, "non_retryable_provider_code", lambda text: provider_code)

        assert llm_judge._classify_provider_exception(RuntimeError("boom")) is expected

    def test_classify_survives_a_broken_classifier(self, monkeypatch) -> None:
        def _boom(text: str) -> str:
            raise RuntimeError("classifier down")

        monkeypatch.setattr(model_backend_errors, "non_retryable_provider_code", _boom)

        assert llm_judge._classify_provider_exception(RuntimeError("boom")) is (JudgeErrorCode.JUDGE_UNAVAILABLE)

    def test_a_cancellation_from_the_bridge_is_re_raised(self, monkeypatch) -> None:
        def _cancel(coro: Any) -> Any:
            coro.close()
            raise KeyboardInterrupt

        monkeypatch.setattr(llm_judge, "run_coroutine_sync", _cancel)
        judge = llm_judge.build_model_judge_callable(_FakeHub("ignored"), str(uuid.uuid4()))

        with pytest.raises(KeyboardInterrupt):
            judge({"output": "x"}, _eval_def())

    def test_a_generic_provider_failure_is_coded(self, monkeypatch) -> None:
        def _boom(coro: Any) -> Any:
            coro.close()
            raise RuntimeError("provider exploded")

        monkeypatch.setattr(llm_judge, "run_coroutine_sync", _boom)
        monkeypatch.setattr(model_backend_errors, "non_retryable_provider_code", lambda text: None)
        judge = llm_judge.build_model_judge_callable(_FakeHub("ignored"), str(uuid.uuid4()))

        with pytest.raises(JudgeExecutionError) as exc_info:
            judge({"output": "x"}, _eval_def())

        assert exc_info.value.code is JudgeErrorCode.JUDGE_UNAVAILABLE

    def test_a_typed_config_with_a_non_object_backing_has_no_backend(self) -> None:
        config = {"answer_contract": "typed_questions", "backing": "nope"}

        assert llm_judge._judge_backend_id(config) is None


class TestCorrectionRevalidationDispatch:
    @pytest.mark.asyncio
    async def test_the_model_judge_family_is_dispatched(self) -> None:
        correction = CorrectionDefinition(
            id="corr",
            guardrail_id="guard",
            model_backend_id=str(uuid.uuid4()),
            revalidation_detector_family=CorrectionDetectorFamily.MODEL_JUDGE,
            revalidation_model_backend_id=str(uuid.uuid4()),
        )

        def _judge(output: dict[str, Any], eval_def: EvalDefinition) -> dict[str, Any]:
            return {"passed": True, "score": 1.0, "detail": "clean"}

        result = await _run_different_family_revalidation(
            EvalEngine(),
            {"body": "safe content"},
            family=CorrectionDetectorFamily.MODEL_JUDGE,
            correction=correction,
            revalidation_config=None,
            judge_callable=_judge,
        )

        assert result.passed is True


class TestMcpExceptionError:
    def test_a_structured_422_detail_reaches_the_caller(self) -> None:
        exc = StarletteHTTPException(
            status_code=422,
            detail={"code": "judge_config_invalid", "message": "bad config", "hint": "fix it"},
        )

        payload = _tool_exception_error("fallback", exc, "tool.log")

        assert payload == {
            "error": "validation_failed",
            "code": "judge_config_invalid",
            "detail": "bad config",
            "hint": "fix it",
        }

    def test_a_plain_detail_keeps_the_operation_message(self) -> None:
        exc = StarletteHTTPException(status_code=503, detail="upstream down")

        payload = _tool_exception_error("operation context", exc, "tool.log")

        assert payload == {"error": "server_error", "detail": "operation context"}
