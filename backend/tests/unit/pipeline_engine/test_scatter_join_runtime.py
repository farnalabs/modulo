"""Runtime (LangGraph wiring) tests for scatter (fan-out) + Join (fan-in).

The pure core is exercised by test_scatter_join.py. These tests round-trip a
REAL scatter -> Join graph through build_graph_from_json + compiled.ainvoke so
the actual LangGraph runtime semantics are verified end-to-end:

  * Gap 1 - each fan-out branch receives ITS OWN item (not an identical copy of
    the parent state), via run_context["input"] / __scatter_item__.
  * Gap 2 - per-branch status is written to __scatter_status__ at runtime so a
    downstream Join can apply its partial-failure policy.
  * Gap 3 - fan_out is honored for sandbox_agent nodes (not silently dropped),
    and composite+fan_out is rejected at compile time (no runtime child factory).

FAR-402 P3 / FAR-417.
"""

import json
import uuid
from typing import Any

import pytest
from langchain_core.messages import BaseMessage

from modulo.core.model_backend_hub import ModelBackendHub
from modulo.core.pipeline_engine.decorator import set_model_backend_hub
from modulo.core.pipeline_engine.graph_cache import build_graph_from_json
from modulo.core.pipeline_engine.scatter_join import (
    JoinConfigurationError,
    validate_scatter_join_node,
)
from modulo.model_backends.base import ModelBackendBase
from modulo.model_backends.stub.backend import StubModelBackend


class _StubAdapter(ModelBackendBase):
    """Adapts StubModelBackend (BaseChatModel) to ModelBackendBase async invoke."""

    def __init__(self, fixture_map: dict[str, str]) -> None:
        self._inner = StubModelBackend(fixture_map)

    async def invoke(self, messages: list[BaseMessage], **kwargs: Any) -> BaseMessage:
        return await self._inner.ainvoke(messages, **kwargs)

    def stream(self, messages: list[BaseMessage], tools: list[dict] | None = None, **kwargs: Any):
        return self._inner.astream(messages, tools=tools, **kwargs)

    @property
    def backend_id(self) -> str:
        return "stub"


class _FailOnItemAdapter(ModelBackendBase):
    """Stub that raises for one specific rendered item so a branch fails."""

    def __init__(self, fail_item: Any, fixture_map: dict[str, str]) -> None:
        self._fail_item = fail_item
        self._inner = StubModelBackend(fixture_map)

    async def invoke(self, messages: list[BaseMessage], **kwargs: Any) -> BaseMessage:
        content = messages[0].content if messages else ""
        if f"process {self._fail_item}" in str(content):
            raise RuntimeError(f"injected failure for item {self._fail_item}")
        return await self._inner.ainvoke(messages, **kwargs)

    def stream(self, messages: list[BaseMessage], tools: list[dict] | None = None, **kwargs: Any):
        return self._inner.astream(messages, tools=tools, **kwargs)

    @property
    def backend_id(self) -> str:
        return "stub"


def _scatter_join_graph_json(
    *,
    scatter_id: str,
    join_id: str,
    scatter_node_type: str,
    backend_id: str | None,
    items_split: str = "items",
) -> dict[str, Any]:
    scatter = {
        "id": scatter_id,
        "node_type": scatter_node_type,
        "agent_id": str(uuid.uuid4()),
        "role": "agent",
        "prompt_template": "process {{ input }}",
        "model_backend_id": backend_id,
        "fan_out": {"split": items_split},
    }
    join = {
        "id": join_id,
        "node_type": "join",
        "collect": [{"node": scatter_id}],
        "aggregate": {"kind": "concat"},
    }
    return {
        "nodes": [scatter, join],
        "edges": [{"source": scatter_id, "target": join_id, "type": "normal"}],
    }


async def _run_with_hub(graph_json: dict[str, Any], state: dict[str, Any], hub: ModelBackendHub) -> dict[str, Any]:
    await hub.__aenter__()
    set_model_backend_hub(hub)
    try:
        compiled = build_graph_from_json(graph_json, session_factory=None, org_id=uuid.uuid4())
        config = {"configurable": {"thread_id": str(uuid.uuid4())}}
        return await compiled.ainvoke(state, config)
    finally:
        set_model_backend_hub(None)
        await hub.__aexit__(None, None, None)


# --------------------------------------------------------------------------- #
# Gap 1 + Gap 2: agent fan-out delivers per-item input and records status
# --------------------------------------------------------------------------- #


async def test_scatter_join_roundtrip_delivers_item_per_branch():
    scatter_id = str(uuid.uuid4())
    join_id = str(uuid.uuid4())
    backend_id = uuid.uuid4()

    hub = ModelBackendHub()
    hub.register(
        backend_id,
        _StubAdapter(
            {
                "process 1": json.dumps({"got": 1}),
                "process 2": json.dumps({"got": 2}),
                "process 3": json.dumps({"got": 3}),
            }
        ),
    )

    state = {
        "items": [1, 2, 3],
        "run_context": {"input": {}, "cancelled": False},
        "artifacts": [],
    }
    result = await _run_with_hub(
        _scatter_join_graph_json(
            scatter_id=scatter_id,
            join_id=join_id,
            scatter_node_type="agent",
            backend_id=str(backend_id),
        ),
        state,
        hub,
    )

    # Gap 1: every branch processed its OWN item (the stub only answers for the
    # exact rendered prompt "process <item>", so a passing assertion proves the
    # item reached the child's prompt/input).
    joined = result[join_id]
    assert sorted(o["output"]["got"] for o in joined) == [1, 2, 3]

    # Gap 2: per-branch status recorded at runtime.
    status = result["__scatter_status__"]
    child_ids = result["__scatter_manifest__"][scatter_id]
    assert len(child_ids) == 3
    assert all(status[c] == "succeeded" for c in child_ids)


async def test_scatter_records_failed_branch_status_and_join_proceeds():
    scatter_id = str(uuid.uuid4())
    join_id = str(uuid.uuid4())
    backend_id = uuid.uuid4()

    hub = ModelBackendHub()
    hub.register(
        backend_id,
        _FailOnItemAdapter(
            fail_item=2,
            fixture_map={
                "process 1": json.dumps({"got": 1}),
                "process 3": json.dumps({"got": 3}),
            },
        ),
    )

    state = {
        "items": [1, 2, 3],
        "run_context": {"input": {}, "cancelled": False},
        "artifacts": [],
    }
    result = await _run_with_hub(
        _scatter_join_graph_json(
            scatter_id=scatter_id,
            join_id=join_id,
            scatter_node_type="agent",
            backend_id=str(backend_id),
        ),
        state,
        hub,
    )

    # Gap 2: the failing branch is marked "failed"; siblings still "succeeded".
    # The scatter does NOT abort - the join proceeds (collect_and_proceed), so
    # __scatter_status__ is what makes partial failure observable downstream.
    status = result["__scatter_status__"]
    assert status[f"{scatter_id}__scatter_0"] == "succeeded"
    assert status[f"{scatter_id}__scatter_1"] == "failed"
    assert status[f"{scatter_id}__scatter_2"] == "succeeded"
    # Join still ran to completion over all three branches.
    assert len(result[join_id]) == 3


# --------------------------------------------------------------------------- #
# Gap 3: fan_out honored for sandbox_agent; composite rejected
# --------------------------------------------------------------------------- #


async def test_sandbox_agent_fan_out_honored_and_delivers_item(monkeypatch: pytest.MonkeyPatch):
    scatter_id = str(uuid.uuid4())
    join_id = str(uuid.uuid4())

    # Replace the real E2B-backed sandbox runner with a stub that echoes the
    # per-branch item. This proves fan_out is honored for sandbox_agent nodes
    # (compiled to N child branches), rather than silently dropped.
    def fake_sandbox_fn(node_def: dict[str, Any], **kwargs: Any) -> Any:
        async def _node(state: dict[str, Any]) -> dict[str, Any]:
            item = (state.get("run_context") or {}).get("input")
            return {
                "artifacts": [{"node_id": node_def["id"], "status": "completed", "output": {"sandbox_got": item}}],
                "output": {"sandbox_got": item},
            }

        return _node

    monkeypatch.setattr(
        "modulo.core.pipeline_engine.graph_cache.make_sandbox_agent_fn",
        fake_sandbox_fn,
    )

    state = {
        "items": ["a", "b", "c"],
        "run_context": {"input": {}, "cancelled": False},
        "artifacts": [],
    }
    compiled = build_graph_from_json(
        _scatter_join_graph_json(
            scatter_id=scatter_id,
            join_id=join_id,
            scatter_node_type="sandbox_agent",
            backend_id=None,
        ),
        session_factory=None,
        org_id=uuid.uuid4(),
    )
    config = {"configurable": {"thread_id": str(uuid.uuid4())}}
    result = await compiled.ainvoke(state, config)

    joined = result[join_id]
    assert sorted(o["output"]["sandbox_got"] for o in joined) == ["a", "b", "c"]
    status = result["__scatter_status__"]
    assert all(v == "succeeded" for v in status.values())


def test_validate_scatter_join_node_allows_sandbox_agent_fan_out():
    # fan_out on sandbox_agent is a supported, executable shape.
    assert validate_scatter_join_node({"node_type": "sandbox_agent", "fan_out": {"split": "items"}}) is None


def test_validate_scatter_join_node_rejects_composite_fan_out():
    # composite has no runtime child factory; fan_out on it must be rejected so
    # it cannot be silently dropped at runtime.
    with pytest.raises(JoinConfigurationError):
        validate_scatter_join_node({"node_type": "composite", "fan_out": {"split": "items"}})


def test_build_graph_rejects_composite_fan_out():
    graph_json = {
        "nodes": [
            {"id": str(uuid.uuid4()), "node_type": "composite", "fan_out": {"split": "items"}},
        ],
        "edges": [],
    }
    with pytest.raises(JoinConfigurationError):
        build_graph_from_json(graph_json)
