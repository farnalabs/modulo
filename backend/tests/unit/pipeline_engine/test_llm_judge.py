"""Unit tests for the FAR-1652 shared model-judge resolver and sync bridge.

``llm_judge.py`` owns the resolver both eval call sites share plus the two
bounds that keep a hung judge from wedging a run.  The gate-level tests in
``test_hitl_review_llm_judge.py`` prove the end-to-end behaviour; this module
pins the branches they do not reach:

* the malformed ``model_backend_id`` fail-closed judge,
* the non-JSON response fallback,
* ``resolve_model_judge_callable``'s ``None`` guards (non-judge eval, absent hub),
* the caller-side bridge timeout's two arms — a still-queued future is
  cancelled and its coroutine closed, while an already-running future is left
  to the inner :func:`invoke_backend` bound.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessage

import modulo.core.pipeline_engine.llm_judge as llm_judge
from modulo.core.eval_engine import EvalDefinition, EvalType
from modulo.core.eval_engine.typed_judge import JudgeErrorCode, JudgeExecutionError


class _FakeBackend:
    """Fake model backend whose invoke() returns the configured content."""

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


def _make_eval_def(
    *,
    eval_type: EvalType = EvalType.MODEL_JUDGE,
    config: dict[str, Any] | None = None,
) -> EvalDefinition:
    return EvalDefinition(
        id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        node_id="n1",
        name="judge-quality",
        eval_type=eval_type,
        config=config or {"field": "content"},
        failure_behaviour="warn",
    )


# ---------------------------------------------------------------------------
# build_model_judge_callable: configuration + response failure branches
# ---------------------------------------------------------------------------


def test_malformed_model_backend_id_is_a_configuration_error() -> None:
    """A non-UUID ``model_backend_id`` yields a coded configuration error.

    It never raises out of the eval loop uncaught — the engine turns the coded
    error into ``outcome=error`` (no verdict), so sibling evals still run.
    """
    judge = llm_judge.build_model_judge_callable(_FakeHub("ignored"), "not-a-uuid")

    with pytest.raises(JudgeExecutionError) as exc_info:
        judge({"content": "some output"}, _make_eval_def())

    assert exc_info.value.code is JudgeErrorCode.JUDGE_CONFIGURATION
    assert "malformed" in exc_info.value.detail
    assert "not-a-uuid" in exc_info.value.detail


def test_non_json_response_is_a_malformed_response_error() -> None:
    """Prose (not JSON) is a malformed response with no verdict.

    The raw model text is never copied into the human-readable detail — the
    detail carries the instrument failure, not the payload.
    """
    judge = llm_judge.build_model_judge_callable(
        _FakeHub("this is not json"),
        str(uuid.uuid4()),
    )

    with pytest.raises(JudgeExecutionError) as exc_info:
        judge({"content": "some output"}, _make_eval_def())

    assert exc_info.value.code is JudgeErrorCode.JUDGE_MALFORMED_RESPONSE
    assert "non-JSON" in exc_info.value.detail
    assert "this is not json" not in exc_info.value.detail


@pytest.mark.parametrize(
    "raw_response",
    [
        '{"passed": "false", "score": 1.0}',
        '{"passed": "no", "score": 1.0}',
        '{"passed": "False", "score": 1.0}',
        '{"passed": "0", "score": 1.0}',
    ],
)
def test_non_boolean_passed_is_a_malformed_response(raw_response: str) -> None:
    """A JSON string that looks falsy is truthy in Python — never coerce it."""
    judge = llm_judge.build_model_judge_callable(_FakeHub(raw_response), str(uuid.uuid4()))

    with pytest.raises(JudgeExecutionError) as exc_info:
        judge({"content": "some output"}, _make_eval_def())

    assert exc_info.value.code is JudgeErrorCode.JUDGE_MALFORMED_RESPONSE


def test_response_detail_null_is_coerced_to_empty_string() -> None:
    """A JSON verdict with ``detail: null`` must not surface a ``None`` detail."""
    judge = llm_judge.build_model_judge_callable(
        _FakeHub('{"passed": true, "score": 1.0, "detail": null}'),
        str(uuid.uuid4()),
    )

    result = judge({"content": "some output"}, _make_eval_def())

    assert result["passed"] is True
    assert not result["detail"]


# ---------------------------------------------------------------------------
# resolve_model_judge_callable: the None guards
# ---------------------------------------------------------------------------


def test_resolve_model_judge_returns_none_for_non_judge_eval() -> None:
    """A non-llm_judge eval never resolves a judge, even with a backend id."""
    eval_def = _make_eval_def(
        eval_type=EvalType.REGEX,
        config={"field": "content", "model_backend_id": str(uuid.uuid4())},
    )

    assert llm_judge.resolve_model_judge_callable(eval_def) is None


def test_resolve_model_judge_returns_none_when_hub_unavailable() -> None:
    """An llm_judge eval with no ambient ModelBackendHub resolves to ``None``
    (the caller then fails the eval closed, pre-existing behaviour)."""
    eval_def = _make_eval_def(
        config={"field": "content", "model_backend_id": str(uuid.uuid4())},
    )

    with patch(
        "modulo.core.pipeline_engine.decorator.get_model_backend_hub",
        return_value=None,
    ):
        assert llm_judge.resolve_model_judge_callable(eval_def) is None


def test_resolve_model_judge_builds_callable_when_hub_present() -> None:
    """With an ambient hub, the resolver returns a working judge backed by it."""
    eval_def = _make_eval_def(
        config={"field": "content", "model_backend_id": str(uuid.uuid4())},
    )
    hub = _FakeHub('{"passed": true, "score": 0.9, "detail": "ok"}')

    with patch(
        "modulo.core.pipeline_engine.decorator.get_model_backend_hub",
        return_value=hub,
    ):
        judge = llm_judge.resolve_model_judge_callable(eval_def)

    assert judge is not None
    result = judge({"content": "some output"}, eval_def)
    assert result["passed"] is True
    assert result["score"] == pytest.approx(0.9)


# ---------------------------------------------------------------------------
# run_coroutine_sync: the two arms of the caller-side bridge timeout
# ---------------------------------------------------------------------------


class _FakeFuture:
    """Future stub that always reports a caller-side timeout."""

    def __init__(self, *, done: bool, cancellable: bool) -> None:
        self._done = done
        self._cancellable = cancellable
        self.cancelled = False

    def result(self, timeout: float | None = None) -> Any:
        raise TimeoutError("bridge bound fired")

    def done(self) -> bool:
        return self._done

    def cancel(self) -> bool:
        self.cancelled = True
        return self._cancellable


class _RecordingExecutor:
    """Executor stub returning a pre-built future from ``submit``."""

    def __init__(self, future: _FakeFuture) -> None:
        self._future = future

    def submit(self, fn: Any) -> _FakeFuture:
        return self._future


class _FakeCoro:
    """Coroutine stub recording whether ``close`` was called."""

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_bridge_timeout_cancels_queued_coroutine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bridge timeout on a future that never started cancels it and closes
    the coroutine (so a never-run coroutine does not warn on GC)."""
    future = _FakeFuture(done=False, cancellable=True)
    monkeypatch.setattr(llm_judge, "_JUDGE_EXECUTOR", _RecordingExecutor(future))
    coro = _FakeCoro()

    with pytest.raises(TimeoutError):
        llm_judge.run_coroutine_sync(coro)  # type: ignore[arg-type]

    assert future.cancelled is True
    assert coro.closed is True


def test_bridge_timeout_leaves_running_coroutine_to_inner_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bridge timeout on an already-running future cannot cancel it; the
    worker thread is left to the inner ``invoke_backend`` bound, and the
    caller still sees the ``TimeoutError``."""
    future = _FakeFuture(done=False, cancellable=False)
    monkeypatch.setattr(llm_judge, "_JUDGE_EXECUTOR", _RecordingExecutor(future))
    coro = _FakeCoro()

    with pytest.raises(TimeoutError):
        llm_judge.run_coroutine_sync(coro)  # type: ignore[arg-type]

    assert future.cancelled is True
    assert coro.closed is False


def test_a_typed_config_ignores_a_top_level_model_backend_id() -> None:
    """``backing.model_backend_id`` is the only source for a typed config."""
    backing_id = uuid.uuid4()
    eval_def = _make_eval_def(
        config={
            "answer_contract": "typed_questions",
            "field": "content",
            "model_backend_id": str(uuid.uuid4()),
            "backing": {"kind": "model_backend", "model_backend_id": str(backing_id)},
        }
    )

    assert llm_judge._judge_backend_id(eval_def.config) == str(backing_id)


def test_a_typed_config_without_a_backing_id_does_not_fall_back_to_the_top_level() -> None:
    eval_def = _make_eval_def(
        config={
            "answer_contract": "typed_questions",
            "field": "content",
            "model_backend_id": str(uuid.uuid4()),
            "backing": {"kind": "model_backend"},
        }
    )

    assert llm_judge._judge_backend_id(eval_def.config) is None
