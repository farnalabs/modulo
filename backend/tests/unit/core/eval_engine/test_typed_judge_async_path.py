"""Async judge path for the two high-volume call sites (ADR 066 D10).

The pipeline post-node loop and the suite runner both run inside ``asyncio``.
An async-capable judge is awaited on that same loop, so no sync bridge, worker
thread or foreign-loop client is involved.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from modulo.core.eval_engine import EvalDefinition, EvalEngine, EvalType
from modulo.core.eval_engine.execute_suite_run import _case_results_async
from modulo.core.pipeline_engine.eval_persist_order import EvalDefDTO, run_evals_persist_before_decide


def _eval_def(*, eval_type: EvalType = EvalType.MODEL_JUDGE) -> EvalDefinition:
    return EvalDefinition(
        id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        node_id="node-1",
        name="judge",
        eval_type=eval_type,
        config={
            "answer_contract": "typed_questions",
            "field": "output",
            "backing": {"kind": "model_backend", "model_backend_id": str(uuid.uuid4())},
            "questions": [{"id": "q", "type": "noul", "weight": 1.0}],
            "pass_rule": {"kind": "weighted_score_at_least", "threshold": 0.5},
        },
        failure_behaviour="warn",
    )


class _AsyncJudge:
    def __init__(self) -> None:
        self.calls = 0

    async def ajudge(self, output: dict[str, Any], eval_def: EvalDefinition) -> dict[str, Any]:
        self.calls += 1
        return {"outcome": "pass", "score": 1.0, "detail": "async verdict"}

    def __call__(self, output: dict[str, Any], eval_def: EvalDefinition) -> dict[str, Any]:
        raise AssertionError("the async path must not call the sync judge")


class TestEvaluateResultAsync:
    async def test_the_engine_awaits_the_async_judge(self) -> None:
        judge = _AsyncJudge()

        result = await EvalEngine().evaluate_result_async(
            {"output": "agent output"},
            _eval_def(),
            model_judge_callable=judge,
        )

        assert judge.calls == 1
        assert result.passed is True
        assert result.detail == "async verdict"

    async def test_a_non_judge_eval_delegates_to_the_sync_path(self) -> None:
        eval_def = EvalDefinition(
            id=uuid.uuid4(),
            org_id=uuid.uuid4(),
            name="regex",
            eval_type=EvalType.REGEX,
            config={"pattern": "ok", "field": "output"},
            failure_behaviour="warn",
        )

        result = await EvalEngine().evaluate_result_async({"output": "ok"}, eval_def)

        assert result.passed is True


class TestPersistOrderUsesTheAsyncPath:
    async def test_an_async_capable_judge_is_awaited_not_bridged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        eval_def = EvalDefDTO(
            id=uuid.uuid4(),
            org_id=uuid.uuid4(),
            name="judge",
            eval_type="model_judge",
            config=_eval_def().config,
            failure_behaviour="warn",
            node_id="node-1",
        )
        judge = _AsyncJudge()

        def _sync_should_not_run(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("the sync compute path must not run for an async judge")

        monkeypatch.setattr(EvalEngine, "evaluate_result", _sync_should_not_run)

        results = await run_evals_persist_before_decide(
            eval_defs=[eval_def],
            resolve_eval_target=lambda _def: {"output": "agent output"},
            resolve_model_judge=lambda _def: judge,
        )

        assert judge.calls == 1
        assert results["judge"].passed is True


class TestSuiteRunUsesTheAsyncPath:
    async def test_the_case_runner_awaits_the_async_judge(self, monkeypatch: pytest.MonkeyPatch) -> None:
        judge = _AsyncJudge()
        run = SimpleNamespace(id=uuid.uuid4(), organisation_id=uuid.uuid4())
        case = SimpleNamespace(id=uuid.uuid4(), input_payload={"content": "agent output"})

        def _sync_should_not_run(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("the sync compute path must not run for an async judge")

        monkeypatch.setattr(EvalEngine, "evaluate_result", _sync_should_not_run)

        results, errored = await _case_results_async(
            run,  # type: ignore[arg-type]
            case,  # type: ignore[arg-type]
            [_eval_def()],
            EvalEngine(),
            judge,
            None,
            eval_definition_version=1,
        )

        assert judge.calls == 1
        assert errored == 0
        assert len(results) == 1
        assert results[0].outcome == "pass"
