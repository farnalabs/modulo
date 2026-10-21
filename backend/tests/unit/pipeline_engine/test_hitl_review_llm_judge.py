"""Unit tests for llm_judge evals inside a HITL gate (FAR-307).

The judge callable is resolved from ``eval_def.config["model_backend_id"]`` via
the ModelBackendHub and passed to ``engine.evaluate``, so the gate fires only
when the judge actually scores low.

When NO judge can run (no backend id, a hung backend), the engine now records
``outcome=error`` with a NULL score — never ``passed=False/score=0.0``
(FAR-1496, ADR 066 D6). The gate still fails closed and fires; these tests
assert BOTH that it fires and that the engine result is an error, not a verdict.
"""

import asyncio
import json
import uuid
from typing import Any
from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessage
from langgraph.errors import GraphInterrupt
from langgraph.types import Interrupt

import modulo.core.pipeline_engine.llm_judge as llm_judge
from modulo.core.eval_engine import EvalDefinition, EvalEngine, EvalType
from modulo.core.eval_engine.typed_judge import JudgeErrorCode, JudgeExecutionError, JudgeOutcome


class _FakeBackend:
    """Fake model backend whose invoke() returns the configured JSON content."""

    def __init__(self, content: str) -> None:
        self._content = content

    async def invoke(self, messages: list[Any], **kwargs: Any) -> AIMessage:
        return AIMessage(content=self._content)


class _FakeHub:
    """Fake ModelBackendHub returning a single fake backend."""

    def __init__(self, content: str) -> None:
        self._backend = _FakeBackend(content)

    async def get(self, backend_id: uuid.UUID, **kwargs: Any) -> _FakeBackend:
        return self._backend


def _judge_payload(score: float, passed: bool = True) -> str:
    return json.dumps(
        {
            "passed": passed,
            "score": score,
            "detail": "judged",
        }
    )


def _make_eval_def(
    name: str,
    *,
    config: dict[str, Any] | None = None,
) -> EvalDefinition:
    return EvalDefinition(
        id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        node_id="n1",
        name=name,
        eval_type=EvalType.MODEL_JUDGE,
        config=config or {"field": "content"},
        failure_behaviour="warn",
    )


def _run_gate_fn(
    gate_config: dict[str, Any],
    state: dict[str, Any],
    eval_defs: list[EvalDefinition] | None,
) -> bool:
    """Run the HITL gate node fn synchronously; return True if it interrupts."""
    from modulo.core.pipeline_engine.node_runner import make_hitl_review_fn

    node_fn = make_hitl_review_fn(gate_config, eval_definitions=eval_defs)

    async def _run() -> Any:
        return await node_fn(state)

    def _raise_interrupt(value: Any) -> None:
        raise GraphInterrupt((Interrupt(value=value),))

    try:
        with patch(
            "modulo.core.pipeline_engine.node_runner.interrupt",
            _raise_interrupt,
        ):
            asyncio.run(_run())
        return False
    except GraphInterrupt:
        return True


def _run_gate_fn_capturing_results(
    gate_config: dict[str, Any],
    state: dict[str, Any],
    eval_defs: list[EvalDefinition] | None,
) -> tuple[bool, list[Any]]:
    """Run the gate fn and capture every engine result it computed."""
    captured: list[Any] = []
    real = EvalEngine.evaluate_result

    def _spy(self: EvalEngine, *args: Any, **kwargs: Any) -> Any:
        result = real(self, *args, **kwargs)
        captured.append(result)
        return result

    with patch.object(EvalEngine, "evaluate_result", _spy):
        interrupted = _run_gate_fn(gate_config, state, eval_defs)
    return interrupted, captured


def _gate_config(eval_name: str) -> dict[str, Any]:
    return {
        "review_id": "gate-llm",
        "label": "Model judge Gate",
        "description": "Gate that fires when the Model judge scores low",
        "human_only": False,
        "eval_condition": {
            "eval_name": eval_name,
            "threshold": 0.5,
            "operator": "lt",
        },
    }


def _base_state() -> dict[str, Any]:
    return {
        "artifacts": [],
        "_hitl_reviews": [],
        "run_context": {"autonomy_recommendation": "manual_approval"},
        "content": "some agent output",
    }


def test_high_score_does_not_fire_gate() -> None:
    """A judge score of 0.9 against threshold 0.5 (lt) skips the gate."""
    eval_def = _make_eval_def(
        "judge-quality",
        config={"field": "content", "model_backend_id": str(uuid.uuid4())},
    )
    hub = _FakeHub(_judge_payload(0.9))

    with patch(
        "modulo.core.pipeline_engine.decorator.get_model_backend_hub",
        return_value=hub,
    ):
        interrupted = _run_gate_fn(
            _gate_config("judge-quality"),
            _base_state(),
            [eval_def],
        )

    assert interrupted is False


def test_low_score_fires_gate() -> None:
    """A judge score of 0.2 against threshold 0.5 (lt) fires the gate."""
    eval_def = _make_eval_def(
        "judge-quality",
        config={"field": "content", "model_backend_id": str(uuid.uuid4())},
    )
    hub = _FakeHub(_judge_payload(0.2))

    with patch(
        "modulo.core.pipeline_engine.decorator.get_model_backend_hub",
        return_value=hub,
    ):
        interrupted = _run_gate_fn(
            _gate_config("judge-quality"),
            _base_state(),
            [eval_def],
        )

    assert interrupted is True


def test_no_model_backend_id_records_error_outcome_and_fires_gate() -> None:
    """Without a model_backend_id the judge produces NO verdict.

    The gate still fails closed and fires, but the result is
    ``outcome=error``/NULL score — not a 0.0 verdict (FAR-1496).
    """
    eval_def = _make_eval_def(
        "judge-quality",
        config={"field": "content"},  # no model_backend_id
    )

    interrupted, captured = _run_gate_fn_capturing_results(
        _gate_config("judge-quality"),
        _base_state(),
        [eval_def],
    )

    assert interrupted is True
    assert captured
    assert captured[0].outcome is JudgeOutcome.ERROR
    assert captured[0].score is None
    assert captured[0].passed is False


# ---------------------------------------------------------------------------
# FAR-1652: the shared judge resolver bounds a hung model backend
# ---------------------------------------------------------------------------


class _HangingBackend:
    """Fake model backend whose invoke() never answers (a hung provider)."""

    async def invoke(self, messages: list[Any], **kwargs: Any) -> AIMessage:
        # Never reached in a passing run: the invoke bound
        # (BACKEND_INVOKE_TIMEOUT_SECONDS) cancels this coroutine. Without
        # that bound it sleeps for the full hour and wedges the bridge
        # pool's single worker thread.
        await asyncio.sleep(3600)
        return AIMessage(content="unreachable")


class _HangingHub:
    """Fake ModelBackendHub returning a backend that never answers."""

    def __init__(self) -> None:
        self._backend = _HangingBackend()

    async def get(self, backend_id: uuid.UUID, **kwargs: Any) -> _HangingBackend:
        return self._backend


class _HungResolveHub:
    """Fake ModelBackendHub whose ``get`` itself never answers (FAR-1652).

    Reproduces the failover-event-emit stall: the hang is in the RESOLVE, not
    in ``backend.invoke``, so it sits outside an invoke-only bound.

    The sleep is 30s - comfortably past the 0.2s resolve bound and the 2s
    bridge backstop used by these tests (so the coroutine is genuinely still
    running when the caller-side bridge timeout would fire), but short enough
    that a no-fix regression run's stuck ``max_workers=1`` bridge thread drains
    on its own instead of wedging interpreter exit for an hour.
    """

    async def get(self, backend_id: uuid.UUID, **kwargs: Any) -> _FakeBackend:
        await asyncio.sleep(30)
        return _FakeBackend(_judge_payload(0.9))


def _shrink_timeout_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep these tests fast: 0.2s resolve+invoke bound, 2s bridge backstop."""
    monkeypatch.setattr(llm_judge, "BACKEND_INVOKE_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(llm_judge, "SYNC_BRIDGE_TIMEOUT_SECONDS", 2.0)


def test_hung_judge_times_out_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A judge whose backend never answers records an ERROR outcome (no score).

    FAR-1652: ``future.result()`` had no timeout, so a hung judge blocked the
    pipeline event loop indefinitely. With the bound the call returns a
    fail-closed result instead - an unjudged output must not pass a gate.

    Discriminating: without the bound this call blocks in the judge until the
    pytest-timeout guard kills the test.
    """
    _shrink_timeout_bounds(monkeypatch)
    eval_def = _make_eval_def(
        "judge-quality",
        config={"field": "content", "model_backend_id": str(uuid.uuid4())},
    )
    judge = llm_judge.build_model_judge_callable(_HangingHub(), str(uuid.uuid4()))

    with pytest.raises(JudgeExecutionError) as exc_info:
        judge({"content": "some agent output"}, eval_def)

    assert exc_info.value.code is JudgeErrorCode.JUDGE_TIMEOUT
    assert "timed out" in exc_info.value.detail


def test_hung_judge_releases_the_bridge_for_the_next_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """The hung invoke is CANCELLED, so the max_workers=1 bridge stays usable.

    A caller-side ``future.result(timeout=...)`` alone would time out the
    CALLER while the worker thread stayed stuck inside the hung coroutine -
    every later judge call would then fail closed too. The inner
    ``asyncio.wait_for`` bound is what frees the thread; this proves it by
    judging successfully with a healthy backend straight after a timeout.
    """
    _shrink_timeout_bounds(monkeypatch)
    eval_def = _make_eval_def(
        "judge-quality",
        config={"field": "content", "model_backend_id": str(uuid.uuid4())},
    )
    hung_judge = llm_judge.build_model_judge_callable(_HangingHub(), str(uuid.uuid4()))
    healthy_judge = llm_judge.build_model_judge_callable(
        _FakeHub(_judge_payload(0.9)),
        str(uuid.uuid4()),
    )

    with pytest.raises(JudgeExecutionError) as exc_info:
        hung_judge({"content": "some agent output"}, eval_def)
    second = healthy_judge({"content": "some agent output"}, eval_def)

    assert exc_info.value.code is JudgeErrorCode.JUDGE_TIMEOUT
    assert second["passed"] is True
    assert second["score"] == pytest.approx(0.9)


def test_hung_hub_get_is_bounded_and_releases_the_bridge_for_the_next_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hung ``hub.get`` RESOLVE is bounded too, and frees the bridge thread.

    ``invoke_backend`` used to bound only ``backend.invoke`` with
    ``asyncio.wait_for`` and left ``await hub.get(backend_id)`` outside it.  A
    hung resolve (e.g. a stalled failover-event emit) therefore kept the
    coroutine running: the caller-side bridge timeout fired, but
    ``future.cancel()`` returns ``False`` on an already-running future, so the
    ``max_workers=1`` ``_JUDGE_EXECUTOR`` thread was never released and every
    subsequent judge call blocked for the full bridge bound and failed closed
    until a process restart.

    Discriminating: without the bound on ``hub.get`` the FIRST call still looks
    fine (the bridge backstop fail-closes it), but the second - healthy - judge
    call cannot get the single bridge thread and fails closed too, so
    ``second["passed"] is True`` fails.
    """
    _shrink_timeout_bounds(monkeypatch)
    eval_def = _make_eval_def(
        "judge-quality",
        config={"field": "content", "model_backend_id": str(uuid.uuid4())},
    )
    hung_resolve_judge = llm_judge.build_model_judge_callable(
        _HungResolveHub(),
        str(uuid.uuid4()),
    )
    healthy_judge = llm_judge.build_model_judge_callable(
        _FakeHub(_judge_payload(0.9)),
        str(uuid.uuid4()),
    )

    with pytest.raises(JudgeExecutionError) as exc_info:
        hung_resolve_judge({"content": "some agent output"}, eval_def)
    second = healthy_judge({"content": "some agent output"}, eval_def)

    # Coded timeout — no verdict was produced (an unjudged output must not
    # pass a gate).
    assert exc_info.value.code is JudgeErrorCode.JUDGE_TIMEOUT
    assert "timed out" in exc_info.value.detail
    # The bridge's single worker thread was released, so this healthy call
    # actually reached its backend instead of queueing behind the hang.
    assert second["passed"] is True
    assert second["score"] == pytest.approx(0.9)


def test_hung_judge_records_error_outcome_and_fires_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end through ``make_hitl_review_fn``: a hung judge produces NO
    verdict (``outcome=error``, NULL score) and the ``lt`` gate fires, instead
    of the run hanging on the judge call."""
    _shrink_timeout_bounds(monkeypatch)
    eval_def = _make_eval_def(
        "judge-quality",
        config={"field": "content", "model_backend_id": str(uuid.uuid4())},
    )
    hub = _HangingHub()

    with patch(
        "modulo.core.pipeline_engine.decorator.get_model_backend_hub",
        return_value=hub,
    ):
        interrupted, captured = _run_gate_fn_capturing_results(
            _gate_config("judge-quality"),
            _base_state(),
            [eval_def],
        )

    assert interrupted is True
    assert captured
    assert captured[0].outcome is JudgeOutcome.ERROR
    assert captured[0].score is None
    assert captured[0].passed is False
