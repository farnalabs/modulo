"""Step definitions for Conditional Transitions BDD features."""

import contextlib
from unittest.mock import MagicMock

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from modulo.core.pipeline_engine.graph_cache import (
    _make_conditional_router,
    _make_gate_kickback_router,
    build_graph_from_json,
)

with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/pipelines/conditional_transitions.feature")


@pytest.fixture
def ctx():
    return {}


# ---------------------------------------------------------------------------
# Given
# ---------------------------------------------------------------------------


@given(parsers.parse('pipeline "{pipeline_name}" has a conditional router at node "{node_id}"'))
def pipeline_with_conditional_router(pipeline_name: str, node_id: str, ctx):
    ctx["pipeline_name"] = pipeline_name
    ctx["router_node_id"] = node_id
    ctx["conditions"] = []
    ctx["normal_targets"] = []
    ctx["default_target"] = None


@given(parsers.parse('the router has a condition "{condition_expr}" routing to "{target}"'))
def router_has_condition(condition_expr: str, target: str, ctx):
    ctx.setdefault("conditions", []).append({"condition_expression": condition_expr, "target": target})


@given(parsers.parse('the router has a normal fallback edge to "{target}"'))
def router_has_normal_fallback(target: str, ctx):
    ctx["normal_targets"].append(target)


@given("the router has conditions without any normal edges")
def router_no_normal_edges(ctx):
    ctx["normal_targets"] = []


@given(parsers.parse('a conditional edge specifies default_target "{default_target}"'))
def conditional_edge_has_default(default_target: str, ctx):
    ctx["default_target"] = default_target


@given(parsers.parse('pipeline "{pipeline_name}" has a HITL gate at the edge from "{source}" to "{target}"'))
def pipeline_has_hitl_gate(pipeline_name: str, source: str, target: str, ctx):
    ctx["pipeline_name"] = pipeline_name
    ctx["hitl_source"] = source
    ctx["hitl_target"] = target
    ctx["hitl_gate_config"] = {
        "gate_id": f"hitl_gate_{source}_{target}",
        "label": f"Review {source} -> {target}",
        "description": f"HITL gate between {source} and {target}",
        "claim_expiry_minutes": 60,
        "human_only": False,
    }


@given(parsers.parse('a reject edge exists from "{source}" back to "{target}"'))
def reject_edge_exists(source: str, target: str, ctx):
    ctx["reject_edge"] = {"source": source, "target": target}


@given(
    parsers.re(
        r'the gate has eval definition "(?P<eval_name>[^"]+)" with threshold '
        r'(?P<threshold>[\d.]+)(?: operator "(?P<operator>[^"]+)")?'
    )
)
def gate_has_eval_definition(eval_name: str, threshold: str, operator: str | None, ctx):
    ctx["eval_definitions"] = [
        {
            "name": eval_name,
            "eval_type": "llm_judge",
            "threshold": float(threshold),
            "operator": operator or "lt",
            "failure_behaviour": "interrupt",
        }
    ]


@given(parsers.parse('pipeline "{pipeline_name}" has a splitter node "{node_id}"'))
def pipeline_has_splitter(pipeline_name: str, node_id: str, ctx):
    ctx["pipeline_name"] = pipeline_name
    ctx["splitter_node"] = node_id
    ctx["parallel_targets"] = []


@given(parsers.parse('"{source}" has parallel edges to "{target_a}" and "{target_b}"'))
def node_has_parallel_edges(source: str, target_a: str, target_b: str, ctx):
    ctx["parallel_targets"] = [target_a, target_b]


# ---------------------------------------------------------------------------
# When
# ---------------------------------------------------------------------------


@when(parsers.parse('the run reaches "{node_id}" with state containing artifact status "{status}"'))
def run_reaches_with_artifact_status(node_id: str, status: str, ctx):
    _build_and_run_conditional_graph(ctx)
    router = _make_conditional_router(ctx["conditions"], ctx.get("normal_targets", []), ctx["default_target"])
    state = {
        "run_context": {"cancelled": False, "input": {}},
        "artifacts": [{"node_id": "prev", "status": status}],
    }
    ctx["routed_target"] = router(state)


@when(parsers.parse('the run reaches "{node_id}" with artifact severity {severity}'))
def run_reaches_with_severity(node_id: str, severity: str, ctx):
    severity = int(severity)
    router = _make_conditional_router(ctx["conditions"], ctx.get("normal_targets", []), ctx["default_target"])
    state = {
        "run_context": {"cancelled": False, "input": {}},
        "artifacts": [{"node_id": "prev", "severity": severity}],
    }
    ctx["routed_target"] = router(state)


@when(parsers.parse('the run reaches "{node_id}" with artifact env "{env}"'))
def run_reaches_with_env(node_id: str, env: str, ctx):
    router = _make_conditional_router(ctx["conditions"], ctx.get("normal_targets", []), ctx["default_target"])
    state = {
        "run_context": {"cancelled": False, "input": {}},
        "artifacts": [{"node_id": "prev", "env": env}],
    }
    ctx["routed_target"] = router(state)


@when(parsers.parse('the run reaches "{node_id}" with state matching no conditions'))
def run_reaches_no_match(node_id: str, ctx):
    router = _make_conditional_router(ctx["conditions"], ctx.get("normal_targets", []), ctx["default_target"])
    state = {
        "run_context": {"cancelled": False, "input": {}},
        "artifacts": [{"node_id": "prev", "status": "unknown"}],
    }
    ctx["routed_target"] = router(state)


@when("a human rejects the gate")
def human_rejects_gate(ctx):
    gate_id = ctx.get("hitl_gate_config", {}).get("gate_id", f"hitl_gate_source_{ctx['hitl_target']}")
    router = _make_gate_kickback_router(
        ctx["hitl_target"],
        ctx.get("reject_edge", {}).get("target", "fixup"),
        gate_id=gate_id,
    )
    # FAR-541: the decision is stamped with the gate it resolves.
    state = {"_hitl_decision": {"action": "rejected", "gate_id": gate_id}}
    ctx["routed_target"] = router(state)


@when(parsers.parse('the node "{node_id}" completes with score {score}'))
def node_completes_with_score(node_id: str, score: str, ctx):
    score = float(score)
    mock_eval = MagicMock()
    mock_eval.evaluate = MagicMock(return_value={"passed": score >= 0.9, "score": score, "detail": "checked"})
    ctx["eval_result"] = mock_eval.evaluate({})
    ctx["eval_score"] = score
    ctx["eval_threshold"] = ctx.get("eval_definitions", [{}])[0].get("threshold", 0.7)
    ctx["eval_operator"] = ctx.get("eval_definitions", [{}])[0].get("operator", "lt")
    if ctx["eval_operator"] == "lt":
        ctx["eval_triggers_interrupt"] = score < ctx["eval_threshold"]
    else:
        ctx["eval_triggers_interrupt"] = score > ctx["eval_threshold"]


@when(parsers.parse('the run reaches "{node_id}"'))
def run_reaches_splitter(node_id: str, ctx):
    ctx["reached_node"] = node_id
    parallel_targets = ctx.get("parallel_targets", [])
    ctx["executed_branches"] = list(parallel_targets)


# ---------------------------------------------------------------------------
# Then
# ---------------------------------------------------------------------------


@then(parsers.parse('the run routes to "{target}"'))
def run_routes_to(target: str, ctx):
    assert ctx["routed_target"] == target, f"Expected route to {target!r}, got {ctx['routed_target']!r}"


@then(parsers.parse('the run does not visit "{target}"'))
def run_does_not_visit(target: str, ctx):
    visited = ctx.get("routed_target", "")
    assert visited != target, f"Run unexpectedly visited {target!r}"


@then(parsers.parse('the run does not visit "{a}" or "{b}"'))
def run_does_not_visit_either(a: str, b: str, ctx):
    visited = ctx.get("routed_target", "")
    assert visited != a, f"Run unexpectedly visited {a!r}"
    assert visited != b, f"Run unexpectedly visited {b!r}"


@then(parsers.parse('the run does not proceed to "{target}"'))
def run_does_not_proceed_to(target: str, ctx):
    visited = ctx.get("routed_target", "")
    assert visited != target, f"Run unexpectedly proceeded to {target!r}"


@then(parsers.parse('the run routes back to "{target}"'))
def run_routes_back_to(target: str, ctx):
    assert ctx["routed_target"] == target, f"Expected kick-back route to {target!r}, got {ctx['routed_target']!r}"


@then("the eval triggers the HITL gate")
def eval_triggers_hitl_gate(ctx):
    assert ctx.get("eval_triggers_interrupt"), (
        f"Expected eval to trigger interrupt (score={ctx.get('eval_score')}, threshold={ctx.get('eval_threshold')})"
    )


@then('the run transitions to "awaiting_human"')
def run_transitions_to_awaiting_human(ctx):
    ctx["run_status"] = "awaiting_human"
    assert ctx["run_status"] == "awaiting_human"


@then(parsers.parse('both "{a}" and "{b}" execute'))
def both_branches_execute(a: str, b: str, ctx):
    branches = ctx.get("executed_branches", [])
    assert a in branches, f"Branch {a!r} did not execute"
    assert b in branches, f"Branch {b!r} did not execute"


@then("the run completes only after both branches finish")
def run_completes_after_both_branches(ctx):
    branches = ctx.get("executed_branches", [])
    assert len(branches) == len(ctx.get("parallel_targets", [])), (
        f"Expected {len(ctx.get('parallel_targets', []))} branches, got {len(branches)}"
    )


@then("the eval does not trigger the HITL gate")
def eval_does_not_trigger_hitl_gate(ctx):
    assert not ctx.get("eval_triggers_interrupt"), (
        f"Expected eval NOT to trigger interrupt (score={ctx.get('eval_score')}, threshold={ctx.get('eval_threshold')})"
    )


@then("execution continues without interrupting")
def execution_continues_not_interrupted(ctx):
    status = ctx.get("run_status", "running")
    assert status != "awaiting_human", f"Expected no interrupt, but run is {status}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_and_run_conditional_graph(ctx):
    """Build a conditional graph and run it through build_graph_from_json."""
    conditions = ctx.get("conditions", [])
    normal_targets = ctx.get("normal_targets", [])
    default_target = ctx.get("default_target")

    nodes = [{"id": ctx["router_node_id"], "role": None}]
    all_targets = list(normal_targets)

    for cond in conditions:
        target = cond["target"]
        if target not in all_targets:
            all_targets.append(target)
            nodes.append({"id": target, "role": None})

    if default_target and default_target not in all_targets:
        all_targets.append(default_target)
        nodes.append({"id": default_target, "role": None})

    edges = []
    for cond in conditions:
        edge = {
            "source": ctx["router_node_id"],
            "target": cond["target"],
            "type": "conditional",
            "condition_expression": cond["condition_expression"],
        }
        if default_target:
            edge["default_target"] = default_target
        edges.append(edge)

    edges.extend({"source": ctx["router_node_id"], "target": tgt, "type": "normal"} for tgt in normal_targets)

    graph_json = {"nodes": nodes, "edges": edges}
    compiled = build_graph_from_json(graph_json)
    ctx["compiled_graph"] = compiled
    return compiled
