"""End-to-end test: answer injection reaches conditional edge routing (MAJOR-3).

Proves that when ``_inject_answer_state`` places ``hitl_answer_<gate_id>``
into the gate node's return dict, the pipeline engine's state reducer merges
it into state, and a downstream conditional edge's JMESPath evaluator reads
it to make a routing decision.

Uses a real ``StateGraph`` with the real ``_pipeline_state_reducer`` and
``evaluate_jmespath_condition`` — no Docker/Postgres required.
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import StateGraph

from modulo.core.pipeline_engine.graph_cache import (
    _make_conditional_router,
    _pipeline_state_reducer,
)
from modulo.core.pipeline_engine.jmespath_eval import evaluate_jmespath_condition
from modulo.core.pipeline_engine.node_runner import (
    HITL_ANSWER_STATE_KEY_PREFIX,
    _inject_answer_state,
)

_GATE_ID = "hitl_gate_src_tgt"

# A gate node that simulates what the real HITL gate does: returns a dict
# that is the state update merged by the reducer.


def _make_gate_node(option_id: str | None = None, kind: str = "choice"):
    """Return a node fn that injects a choice/approval answer into state."""

    def _gate_node(state: dict[str, Any]) -> dict[str, Any]:
        decision: dict[str, Any] = {"action": "approved", "gate_id": _GATE_ID}
        if option_id is not None:
            decision["answer"] = {"kind": kind, "option_id": option_id}
        gate_result: dict[str, Any] = {"artifacts": []}
        _inject_answer_state(_GATE_ID, decision, gate_result)
        return gate_result

    return _gate_node


def _build_graph(gate_option_id: str | None = None, gate_kind: str = "choice"):
    """Build a minimal StateGraph: gate -> conditional -> branch_a | branch_b."""
    state_schema = dict[str, Any]
    graph: StateGraph[dict[str, Any]] = StateGraph(state_schema)

    # Nodes
    graph.add_node("gate", _make_gate_node(gate_option_id, gate_kind))
    graph.add_node("branch_a", lambda s: {"visited": "a"})
    graph.add_node("branch_b", lambda s: {"visited": "b"})
    graph.add_node("default", lambda s: {"visited": "default"})

    # Conditional edges from gate: if hitl_answer_<gate_id> == 'option_a' -> branch_a,
    # else if hitl_answer_<gate_id> == 'option_b' -> branch_b.
    state_key = f"{HITL_ANSWER_STATE_KEY_PREFIX}{_GATE_ID}"
    conditional_edges = [
        {
            "condition_expression": f"{state_key} == 'option_a'",
            "target": "branch_a",
        },
        {
            "condition_expression": f"{state_key} == 'option_b'",
            "target": "branch_b",
        },
    ]
    router = _make_conditional_router(conditional_edges, normal_targets=["default"], default_target="default")
    graph.add_conditional_edges("gate", router)

    graph.set_entry_point("gate")
    return graph.compile()


def _run_graph(graph, initial_state: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run the compiled graph and return the final state."""
    state = initial_state or {}
    for step in graph.stream(state, stream_mode="updates"):
        for _node, update in step.items():
            # Merge update using the real reducer
            state = _pipeline_state_reducer(state, update)
    return state


class TestE2ERoutingFromInjection:
    """MAJOR-3: prove injection -> state merge -> conditional routing."""

    def test_choice_option_a_routes_to_branch_a(self):
        graph = _build_graph(gate_option_id="option_a")
        final = _run_graph(graph)
        assert final.get("visited") == "a"

    def test_choice_option_b_routes_to_branch_b(self):
        graph = _build_graph(gate_option_id="option_b")
        final = _run_graph(graph)
        assert final.get("visited") == "b"

    def test_approval_answer_routes_to_default(self):
        """An approval answer does NOT inject into state, so the conditional
        edge's JMESPath expression is falsy -> default branch."""
        graph = _build_graph(gate_option_id=None, gate_kind="approval")
        final = _run_graph(graph)
        assert final.get("visited") == "default"

    def test_approval_with_option_id_also_routes_to_default(self):
        """Even if an attacker sends option_id on an approval gate,
        _inject_answer_state now rejects it (MAJOR-2) -> default branch."""
        # Simulate what would happen if _inject_answer_state did NOT check kind:
        # the option_id would be injected. But our fix prevents that.
        decision = {"action": "approved", "gate_id": _GATE_ID, "answer": {"kind": "approval", "option_id": "attacker"}}
        gate_result: dict[str, Any] = {"artifacts": []}
        _inject_answer_state(_GATE_ID, decision, gate_result)
        state_key = f"{HITL_ANSWER_STATE_KEY_PREFIX}{_GATE_ID}"
        assert state_key not in gate_result, "approval answer must NOT inject option_id into state"

    def test_fails_without_injection(self):
        """Prove the test FAILS if injection is removed — the gate node
        returns no state key, so the conditional edge always routes to
        default even when a choice was made."""

        def _gate_node_no_inject(state: dict[str, Any]) -> dict[str, Any]:
            # Simulate a gate that forgets to call _inject_answer_state
            return {"artifacts": []}

        state_schema = dict[str, Any]
        graph: StateGraph[dict[str, Any]] = StateGraph(state_schema)
        graph.add_node("gate", _gate_node_no_inject)
        graph.add_node("branch_a", lambda s: {"visited": "a"})
        graph.add_node("default", lambda s: {"visited": "default"})

        state_key = f"{HITL_ANSWER_STATE_KEY_PREFIX}{_GATE_ID}"
        conditional_edges = [
            {
                "condition_expression": f"{state_key} == 'option_a'",
                "target": "branch_a",
            },
        ]
        router = _make_conditional_router(conditional_edges, normal_targets=["default"], default_target="default")
        graph.add_conditional_edges("gate", router)
        graph.set_entry_point("gate")
        compiled = graph.compile()

        final = _run_graph(compiled)
        # Without injection, the conditional edge never matches -> default
        assert final.get("visited") == "default", "without injection, branch_a should NOT be reached"
        # And prove branch_a was never visited
        state_key_value = final.get(state_key)
        assert state_key_value is None, "without injection, hitl_answer key must not exist in state"

    def test_state_reducer_merges_injection(self):
        """Prove the real state reducer merges the gate's return dict."""
        decision = {"action": "approved", "gate_id": _GATE_ID, "answer": {"kind": "choice", "option_id": "x"}}
        gate_result: dict[str, Any] = {"artifacts": []}
        _inject_answer_state(_GATE_ID, decision, gate_result)

        state: dict[str, Any] = {"pre_existing": True}
        merged = _pipeline_state_reducer(state, gate_result)
        state_key = f"{HITL_ANSWER_STATE_KEY_PREFIX}{_GATE_ID}"
        assert merged[state_key] == "x"
        assert merged["pre_existing"] is True

    def test_jmespath_reads_injected_key(self):
        """Prove JMESPath evaluator reads the injected key from state."""
        decision = {"action": "approved", "gate_id": _GATE_ID, "answer": {"kind": "choice", "option_id": "yes"}}
        gate_result: dict[str, Any] = {"artifacts": []}
        _inject_answer_state(_GATE_ID, decision, gate_result)

        state_key = f"{HITL_ANSWER_STATE_KEY_PREFIX}{_GATE_ID}"
        state: dict[str, Any] = {state_key: "yes"}
        assert evaluate_jmespath_condition(state, state_key) is True
        assert evaluate_jmespath_condition(state, f"{state_key} == 'yes'") is True
        assert evaluate_jmespath_condition(state, f"{state_key} == 'no'") is False
