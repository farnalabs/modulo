"""Unit coverage for the System Maps typed-judge adapter."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from modulo.core.eval_engine import EvalDefinition, EvalType
from modulo.core.eval_engine.discovery_judge import build_discovery_model_judge_callable

_BACKEND_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_EVAL_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")


class _Backend:
    def __init__(self, *, native: bool = True) -> None:
        self.supports_native_structured_output = native
        self.calls: list[dict[str, Any]] = []

    async def invoke(
        self,
        messages: list[Any],
        output_schema: dict[str, Any] | None = None,
    ) -> SimpleNamespace:
        self.calls.append({"messages": messages, "output_schema": output_schema})
        return SimpleNamespace(content="judged")


class _Hub:
    def __init__(self, backend: _Backend) -> None:
        self._backend = backend
        self.requested: list[uuid.UUID] = []

    async def get(self, backend_id: uuid.UUID) -> _Backend:
        self.requested.append(backend_id)
        return self._backend


class _Verdict:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def to_json(self) -> dict[str, Any]:
        return self._payload


def _eval_definition() -> EvalDefinition:
    return EvalDefinition(
        id=_EVAL_ID,
        org_id=_ORG_ID,
        name="judge",
        eval_type=EvalType.MODEL_JUDGE,
        config={
            "answer_contract": "typed_questions",
            "questions": [{"id": "grounded", "type": "noul", "weight": 1.0}],
            "pass_rule": {"kind": "weighted_score_at_least", "threshold": 0.5},
            "_judge_content": "content",
        },
    )


async def test_ajudge_returns_transport_json_and_supports_both_invocation_shapes() -> None:
    backend = _Backend(native=True)
    hub = _Hub(backend)
    verdict = _Verdict({"outcome": "pass"})
    captured: dict[str, Any] = {}

    async def _judge_questions(handle: Any, *_args: Any, **_kwargs: Any) -> _Verdict:
        captured["handle"] = handle
        await handle.invoke([], None)
        await handle.invoke([], {"type": "object"})
        return verdict

    with patch(
        "modulo.core.eval_engine.discovery_judge.judge_questions",
        new=_judge_questions,
    ):
        result = await build_discovery_model_judge_callable(hub, _BACKEND_ID).ajudge(
            {"candidate": {"nodes": [], "relationships": []}},
            _eval_definition(),
        )

    assert result == {"outcome": "pass"}
    assert hub.requested == [_BACKEND_ID]
    assert backend.calls == [
        {"messages": [], "output_schema": None},
        {"messages": [], "output_schema": {"type": "object"}},
    ]
    handle = captured["handle"]
    assert handle.key == str(_BACKEND_ID)
    assert handle.supports_native_structured_output is True


async def test_ajudge_reports_no_native_structured_output() -> None:
    backend = _Backend(native=False)
    hub = _Hub(backend)
    captured: dict[str, Any] = {}

    async def _judge_questions(handle: Any, *_args: Any, **_kwargs: Any) -> _Verdict:
        captured["handle"] = handle
        return _Verdict({"outcome": "fail"})

    with patch(
        "modulo.core.eval_engine.discovery_judge.judge_questions",
        new=_judge_questions,
    ):
        result = await build_discovery_model_judge_callable(hub, _BACKEND_ID).ajudge(
            {"candidate": {"nodes": [], "relationships": []}},
            _eval_definition(),
        )

    assert result == {"outcome": "fail"}
    assert captured["handle"].supports_native_structured_output is False
