"""Unit tests for scatter (fan-out) + Join (fan-in) — FAR-402 P3 / FAR-417."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from modulo.api.routes.pipelines import GraphPosition, PipelineGraphNode
from modulo.core.pipeline_engine.scatter_join import (
    FanOutCapExceededError,
    FanOutConfig,
    JoinAggregateSpec,
    JoinAggregateUnsupportedError,
    JoinCollectSpec,
    JoinConfigurationError,
    aggregate_join_results,
    child_teardown_dedup_key,
    expand_scatter_nodes,
    run_join_node,
    run_scatter_node,
    scatter_child_node_id,
    validate_scatter_join_node,
)

_POS = GraphPosition(x=0.0, y=0.0)


def _child_id(parent: str, i: int) -> str:
    return scatter_child_node_id(parent, i)


# --------------------------------------------------------------------------- #
# Scatter expansion
# --------------------------------------------------------------------------- #


def test_scatter_expands_to_n_distinct_child_nodes():
    node = {"id": "parent", "node_type": "agent", "fan_out": {"split": "items"}}
    items = [{"x": 1}, {"x": 2}, {"x": 3}]
    children = expand_scatter_nodes(node, items)
    assert len(children) == 3
    ids = [c["id"] for c in children]
    assert ids == [_child_id("parent", 0), _child_id("parent", 1), _child_id("parent", 2)]
    assert len(set(ids)) == 3  # distinct
    # Each child carries its item + correlation metadata, and is no longer a scatter.
    for i, child in enumerate(children):
        assert child["scatter_item"] == items[i]
        assert child["scatter_parent"] == "parent"
        assert child["scatter_index"] == i
        assert "fan_out" not in child


def test_scatter_cap_fail_closed_over_cap():
    node = {
        "id": "parent",
        "node_type": "agent",
        "fan_out": {"split": "items", "max_items": 2},
    }
    items = [1, 2, 3]  # 3 > cap 2
    with pytest.raises(FanOutCapExceededError):
        expand_scatter_nodes(node, items)


def test_scatter_default_cap_is_1000():
    node = {"id": "parent", "node_type": "agent", "fan_out": {"split": "items"}}
    items = list(range(1000))
    # 1000 is within the default cap -> succeeds, 1001 fails.
    assert len(expand_scatter_nodes(node, items)) == 1000
    with pytest.raises(FanOutCapExceededError):
        expand_scatter_nodes(node, list(range(1001)))


async def test_scatter_empty_iterator_vacuous_success():
    calls: list[int] = []

    async def execute_child(child_def):
        calls.append(child_def["scatter_index"])
        return {"ok": True}

    result = await run_scatter_node(
        {"id": "parent", "node_type": "agent", "fan_out": {"split": "items"}},
        items=[],
        execute_child=execute_child,
    )
    assert result == {}
    assert calls == []  # no child executed


async def test_scatter_runs_each_branch_and_isolates_audit_keys():
    executed: list[str] = []

    async def execute_child(child_def):
        executed.append(child_def["id"])  # unique audit/claim/feedback key
        return {"out": child_def["scatter_item"] * 2}

    result = await run_scatter_node(
        {"id": "parent", "node_type": "agent", "fan_out": {"split": "items"}},
        items=[1, 2],
        execute_child=execute_child,
    )
    assert executed == [_child_id("parent", 0), _child_id("parent", 1)]
    assert result[_child_id("parent", 0)] == {"out": 2}
    assert result[_child_id("parent", 1)] == {"out": 4}


# --------------------------------------------------------------------------- #
# Join aggregation
# --------------------------------------------------------------------------- #


def _branch(node_id: str, output: object, status: str = "succeeded") -> dict:
    return {"node_id": node_id, "output": output, "status": status}


def test_join_concat():
    collected = [
        _branch(_child_id("p", 0), "a"),
        _branch(_child_id("p", 1), "b"),
    ]
    out = aggregate_join_results(collected, JoinAggregateSpec(kind="concat"))
    assert out["aggregated"] == ["a", "b"]
    assert out["status"] == "completed"
    assert out["empty"] is False


def test_join_merge_by_key():
    collected = [
        _branch(_child_id("p", 0), {"id": "x", "v": 1}),
        _branch(_child_id("p", 1), {"id": "y", "v": 2}),
    ]
    out = aggregate_join_results(collected, JoinAggregateSpec(kind="merge_by_key", key="id"))
    assert out["aggregated"] == {
        "x": {"id": "x", "v": 1},
        "y": {"id": "y", "v": 2},
    }


def test_join_map():
    collected = [
        _branch(_child_id("p", 0), {"name": "a"}),
        _branch(_child_id("p", 1), {"name": "b"}),
    ]
    out = aggregate_join_results(collected, JoinAggregateSpec(kind="map", map_expression="name"))
    assert out["aggregated"] == ["a", "b"]


def test_join_partial_failure_marks_failed_branch():
    collected = [
        _branch(_child_id("p", 0), "ok"),
        _branch(_child_id("p", 1), None, status="failed"),
    ]
    out = aggregate_join_results(collected, JoinAggregateSpec(kind="concat"))
    assert out["status"] == "partial"
    statuses = {b["node_id"]: b["status"] for b in out["branches"]}
    assert statuses == {_child_id("p", 0): "succeeded", _child_id("p", 1): "failed"}


def test_join_empty_collection_typed_empty():
    out = aggregate_join_results([], JoinAggregateSpec(kind="concat"))
    assert out["empty"] is True
    assert out["status"] == "empty"
    assert out["aggregated"] is None


def test_join_custom_function_deferred():
    with pytest.raises(JoinAggregateUnsupportedError):
        aggregate_join_results([_branch("c", "x")], JoinAggregateSpec(kind="custom_function"))


def test_join_partial_policy_fail_raises():
    collected = [_branch(_child_id("p", 0), None, status="failed")]
    with pytest.raises(JoinConfigurationError):
        aggregate_join_results(
            collected,
            JoinAggregateSpec(kind="concat"),
            partial_policy="fail",
        )


def test_join_merge_by_key_requires_key():
    with pytest.raises(JoinConfigurationError):
        aggregate_join_results([_branch("c", {"id": 1})], JoinAggregateSpec(kind="merge_by_key"))


# --------------------------------------------------------------------------- #
# Teardown idempotency + observability
# --------------------------------------------------------------------------- #


def test_child_teardown_dedup_key_is_run_node_index_scoped():
    a = child_teardown_dedup_key("run-1", "node-1", 3)
    b = child_teardown_dedup_key("run-1", "node-1", 3)
    c = child_teardown_dedup_key("run-1", "node-1", 4)
    assert a == b  # idempotent: same inputs -> same key
    assert a != c


async def test_scatter_events_emitted():
    events: list[str] = []

    def emit(event, **attrs):
        events.append(event)

    await run_scatter_node(
        {"id": "parent", "node_type": "agent", "fan_out": {"split": "items"}},
        items=[1, 2],
        execute_child=lambda c: "x",
        emit_event=emit,
    )
    assert "scatter.start" in events
    assert "scatter.complete" in events


def test_join_events_emitted_partial_and_completed():
    partial: list[str] = []
    completed: list[str] = []

    run_join_node(
        {"id": "j", "node_type": "join", "aggregate": {"kind": "concat"}},
        collected=[_branch("c0", "x"), _branch("c1", None, status="failed")],
        emit_event=lambda e, **a: partial.append(e),
    )
    run_join_node(
        {"id": "j", "node_type": "join", "aggregate": {"kind": "concat"}},
        collected=[_branch("c0", "x")],
        emit_event=lambda e, **a: completed.append(e),
    )
    assert "join.partial" in partial
    assert "join.completed" in completed


# --------------------------------------------------------------------------- #
# Schema-level validation (compile-time fail-closed)
# --------------------------------------------------------------------------- #


def test_join_node_schema_valid():
    node = PipelineGraphNode(
        id="00000000-0000-0000-0000-000000000001",
        position=_POS,
        node_type="join",
        collect=[JoinCollectSpec(node="p")],
        aggregate=JoinAggregateSpec(kind="concat"),
    )
    assert node.node_type == "join"


def test_join_node_requires_collect_and_aggregate():
    with pytest.raises(ValidationError):
        PipelineGraphNode(
            id="00000000-0000-0000-0000-000000000002",
            position=_POS,
            node_type="join",
        )


def test_join_merge_by_key_schema_requires_key():
    with pytest.raises(ValidationError):
        PipelineGraphNode(
            id="00000000-0000-0000-0000-000000000003",
            position=_POS,
            node_type="join",
            collect=[JoinCollectSpec(node="p")],
            aggregate=JoinAggregateSpec(kind="merge_by_key"),
        )


def test_fan_out_allowed_on_agent():
    node = PipelineGraphNode(
        id="00000000-0000-0000-0000-000000000004",
        position=_POS,
        node_type="agent",
        agent_id="00000000-0000-0000-0000-0000000000aa",
        fan_out=FanOutConfig(split="items"),
    )
    assert node.fan_out is not None


def test_fan_out_rejected_on_manual():
    with pytest.raises(ValidationError):
        PipelineGraphNode(
            id="00000000-0000-0000-0000-000000000005",
            position=_POS,
            node_type="manual",
            fan_out=FanOutConfig(split="items"),
        )


def test_join_node_rejects_agent_reference():
    with pytest.raises(ValidationError):
        PipelineGraphNode(
            id="00000000-0000-0000-0000-000000000006",
            position=_POS,
            node_type="join",
            agent_id="00000000-0000-0000-0000-0000000000aa",
            collect=[JoinCollectSpec(node="p")],
            aggregate=JoinAggregateSpec(kind="concat"),
        )


def test_validate_scatter_join_node_helper():
    # join with missing aggregate
    with pytest.raises(JoinConfigurationError):
        validate_scatter_join_node({"node_type": "join", "collect": []})
    # fan_out on manual rejected
    with pytest.raises(JoinConfigurationError):
        validate_scatter_join_node({"node_type": "manual", "fan_out": {"split": "x"}})
    # valid scatter passes
    validate_scatter_join_node({"node_type": "agent", "fan_out": {"split": "items"}})
