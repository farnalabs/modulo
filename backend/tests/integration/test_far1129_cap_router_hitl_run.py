"""FAR-1129 cap-router: real LangGraph execution for Router + HITL nodes.

The router/HITL node unit coverage drives ``make_router_node_fn`` /
``make_hitl_gate_fn`` in isolation. These tests round-trip the nodes through
``build_graph_from_json`` + a real ``compiled.ainvoke`` (the repo's pipeline
runtime), so the wiring that actually reaches those functions — graph
compilation, conditional-edge routing, the synthetic HITL gate node, the
langgraph interrupt, and resumption via ``aupdate_state`` — is exercised for
real. The model backend is a stub registered on a real ``ModelBackendHub``
(no LLM call, no network, no container).

Two execution surfaces:

  * Router rules route a run to the CORRECT leaf branch at runtime, the
    default rule catches un-matched state, and a no-match (classifier) run
    terminalizes with ``RouterNoMatchError`` from the real run.
  * A HITL gate pauses a run with a real langgraph interrupt; resuming with
    the human's decision (via ``aupdate_state`` + ``ainvoke``, the executor's
    resume seam) lets the pipeline complete.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from langchain_core.messages import BaseMessage
from langgraph.checkpoint.memory import InMemorySaver

from modulo.core.model_backend_hub import ModelBackendHub
from modulo.core.pipeline_engine.decorator import set_model_backend_hub
from modulo.core.pipeline_engine.errors import RouterNoMatchError
from modulo.core.pipeline_engine.graph_cache import build_graph_from_json
from modulo.model_backends.base import ModelBackendBase
from modulo.model_backends.stub.backend import StubModelBackend

pytestmark = pytest.mark.integration


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


def _base_state(**overrides: Any) -> dict[str, Any]:
    state: dict[str, Any] = {
        "run_context": {"input": {}, "cancelled": False},
        "artifacts": [],
    }
    state.update(overrides)
    return state


def _agent_node(backend_id: str) -> dict[str, Any]:
    return {
        "id": str(uuid.uuid4()),
        "node_type": "agent",
        "agent_id": str(uuid.uuid4()),
        "role": "agent",
        "prompt_template": "process {{ input.route }}",
        "model_backend_id": backend_id,
    }


def _router_graph_json(
    *,
    router_config: dict[str, Any],
    leaves: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    router_id = str(uuid.uuid4())
    nodes = [{"id": router_id, "node_type": "router", "router_config": router_config}]
    nodes.extend(leaves.values())
    edges = [{"source": router_id, "target": leaf["id"], "type": "normal"} for leaf in leaves.values()]
    return {"nodes": nodes, "edges": edges}


async def _run_graph(graph_json: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    hub = ModelBackendHub()
    fixtures = {
        "process 1": json.dumps({"branch": "one"}),
        "process 2": json.dumps({"branch": "two"}),
        "process 9": json.dumps({"branch": "other"}),
    }
    # Every leaf may declare its own backend id — register the same stub under
    # each so the real hub resolves whatever the graph references.
    for node in graph_json["nodes"]:
        backend_id = node.get("model_backend_id")
        if backend_id:
            hub.register(uuid.UUID(backend_id), _StubAdapter(fixtures))
    await hub.__aenter__()
    set_model_backend_hub(hub)
    try:
        compiled = build_graph_from_json(graph_json, session_factory=None, org_id=uuid.uuid4())
        return await compiled.ainvoke(state)
    finally:
        set_model_backend_hub(None)
        await hub.__aexit__(None, None, None)


def _router_config(*, default_target: str | None, leaves: dict[str, dict[str, Any]]) -> dict[str, Any]:
    # Compile-time validation requires every non-classifier Router to declare
    # an explicit default rule target.
    if default_target is None:
        default_target = leaves["other"]["id"]
    rules = [
        {"guard": "x == `1`", "target": leaves["one"]["id"]},
        {"guard": "x == `2`", "target": leaves["two"]["id"]},
        {"default": True, "target": default_target},
    ]
    return {"rules": rules}


def _route_branch(result: dict[str, Any], leaves: dict[str, dict[str, Any]]) -> str | None:
    artifacts = result.get("artifacts") or []
    if not artifacts:
        return None
    ran = str(artifacts[0].get("node_id"))
    for name, leaf in leaves.items():
        if leaf["id"] == ran:
            return name
    return None


async def test_router_routes_each_branch_real_run() -> None:
    leaves = {
        "one": _agent_node(backend_id=str(uuid.uuid4())),
        "two": _agent_node(backend_id=str(uuid.uuid4())),
        "other": _agent_node(backend_id=str(uuid.uuid4())),
    }
    graph_json = _router_graph_json(router_config=_router_config(default_target=None, leaves=leaves), leaves=leaves)

    result_one = await _run_graph(
        graph_json,
        _base_state(x=1, run_context={"input": {"route": 1}, "cancelled": False}),
    )
    assert _route_branch(result_one, leaves) == "one"

    result_two = await _run_graph(
        graph_json,
        _base_state(x=2, run_context={"input": {"route": 2}, "cancelled": False}),
    )
    assert _route_branch(result_two, leaves) == "two"


async def test_router_default_rule_catches_unmatched_state() -> None:
    leaves = {
        "one": _agent_node(backend_id=str(uuid.uuid4())),
        "two": _agent_node(backend_id=str(uuid.uuid4())),
        "other": _agent_node(backend_id=str(uuid.uuid4())),
    }
    router_config = _router_config(default_target=leaves["other"]["id"], leaves=leaves)
    graph_json = _router_graph_json(router_config=router_config, leaves=leaves)

    result = await _run_graph(
        graph_json,
        _base_state(x=9, run_context={"input": {"route": 9}, "cancelled": False}),
    )
    assert _route_branch(result, leaves) == "other"


async def test_router_no_match_raises_from_real_run() -> None:
    leaves = {
        "one": _agent_node(backend_id=str(uuid.uuid4())),
        "two": _agent_node(backend_id=str(uuid.uuid4())),
    }
    # Classifier mode is allowed to omit a default rule (compile-time validation
    # only requires a default for non-classifier configs), so a genuine
    # no-match is possible at runtime.
    classifier_config = {"mode": "classifier", "rules": [{"label": "b", "target": leaves["two"]["id"]}]}
    graph_json = _router_graph_json(router_config=classifier_config, leaves=leaves)

    with pytest.raises(RouterNoMatchError):
        await _run_graph(
            graph_json,
            _base_state(x=1, _llm_next_node="y", run_context={"input": {"route": 1}, "cancelled": False}),
        )


async def test_hitl_gate_interrupts_then_resumes_and_completes() -> None:
    hub = ModelBackendHub()
    backend_id = str(uuid.uuid4())
    hub.register(uuid.UUID(backend_id), _StubAdapter({"process hello": json.dumps({"out": "ok"})}))
    agent_a = _agent_node(backend_id=backend_id)
    agent_b = _agent_node(backend_id=backend_id)

    graph_json = {
        "nodes": [agent_a, agent_b],
        "edges": [
            {
                "source": agent_a["id"],
                "target": agent_b["id"],
                "type": "normal",
                "hitl_gate_config": {
                    "gate_id": "review_before_b",
                    "label": "review",
                    "description": "review step",
                    "human_only": True,
                    "claim_expiry_minutes": 60,
                    "reject_target": str(uuid.uuid4()),
                },
            }
        ],
    }

    await hub.__aenter__()
    set_model_backend_hub(hub)
    try:
        compiled = build_graph_from_json(graph_json, session_factory=None, org_id=uuid.uuid4())
        compiled.checkpointer = InMemorySaver()
        config = {"configurable": {"thread_id": str(uuid.uuid4())}}
        state = _base_state(run_context={"input": {"route": "hello"}, "cancelled": False})

        paused = await compiled.ainvoke(state, config)
        assert "__interrupt__" in paused

        checkpoint = await compiled.aget_state(config)
        assert checkpoint.next
        interrupt = checkpoint.interrupts[0]
        gate_id = interrupt.value["gate_id"]
        # The compile path re-stamps the gate with a synthetic id derived from
        # the source/target pair — the decision MUST use this (FAR-541).
        assert gate_id == f"hitl_gate_{agent_a['id']}_{agent_b['id']}"
        # The human decision must carry the SAME gate_id the gate stamped
        # (FAR-541) — a mismatched stamp is ignored / not resumed.
        await compiled.aupdate_state(config, {"_hitl_decision": {"action": "approved", "gate_id": gate_id}})
        resumed = await compiled.ainvoke(None, config)

        artifacts = resumed.get("artifacts") or []
        decision = [a for a in artifacts if a.get("result") in ("approved", "rejected")]
        assert decision, "expected a gate decision artifact after resume"
        assert decision[0]["result"] == "approved"
        # Both the pre-gate and post-gate agent nodes completed.
        assert str(agent_b["id"]) in [str(a.get("node_id")) for a in artifacts]
    finally:
        set_model_backend_hub(None)
        await hub.__aexit__(None, None, None)
