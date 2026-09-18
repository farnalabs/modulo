"""Unit tests for llm_judge evals inside a HITL gate (FAR-307).

Prior to the fix, an ``llm_judge`` eval run inside ``make_hitl_gate_fn`` never
received an ``llm_judge_callable``, so ``_evaluate_llm`` returned a fail result
with ``score=0.0`` and a conditional gate referencing it fired on EVERY run.
This suite proves the fix: the judge callable is resolved from
``eval_def.config["model_backend_id"]`` via the ModelBackendHub and passed to
``engine.evaluate``, so the gate fires only when the judge actually scores low.
"""

import asyncio
import json
import uuid
from typing import Any
from unittest.mock import patch

from langchain_core.messages import AIMessage
from langgraph.errors import GraphInterrupt
from langgraph.types import Interrupt

from modulo.core.eval_engine import EvalDefinition, EvalType


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
        eval_type=EvalType.LLM_JUDGE,
        config=config or {"field": "content"},
        failure_behaviour="warn",
    )


def _run_gate_fn(
    gate_config: dict[str, Any],
    state: dict[str, Any],
    eval_defs: list[EvalDefinition] | None,
) -> bool:
    """Run the HITL gate node fn synchronously; return True if it interrupts."""
    from modulo.core.pipeline_engine.node_runner import make_hitl_gate_fn

    node_fn = make_hitl_gate_fn(gate_config, eval_definitions=eval_defs)

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


def _gate_config(eval_name: str) -> dict[str, Any]:
    return {
        "gate_id": "gate-llm",
        "label": "LLM Judge Gate",
        "description": "Gate that fires when the LLM judge scores low",
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
        "_hitl_gates": [],
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


def test_no_model_backend_id_falls_back_to_zero_and_fires_gate() -> None:
    """Without a model_backend_id the judge callable is not built, so the eval
    scores 0.0 (pre-existing behaviour) and the lt gate fires."""
    eval_def = _make_eval_def(
        "judge-quality",
        config={"field": "content"},  # no model_backend_id
    )

    interrupted = _run_gate_fn(
        _gate_config("judge-quality"),
        _base_state(),
        [eval_def],
    )

    assert interrupted is True
