"""BDD step definitions: first-class Router decision nodes (feat-router).

The executing BDD surface for the Router-node behaviours that were previously
pinned by unit tests only (``backend/tests/unit/pipeline_engine/test_router_hitl_nodes.py``)
— the Known Gap this product-map walk closes
(``docs/product-map/pipelines/router-hitl-nodes.md``).

The scenarios drive the real shipped seams end to end:

- ``build_graph_from_json`` — the compile path a saved pipeline graph takes at
  run time, exercising the Router-node compile-time default-rule guard and the
  rule-target entry-point exclusion;
- ``make_router_node_fn`` — the real decision function (first-match-wins over
  the shared JMESPath evaluator, ``default`` rule, classifier label mode);
- ``PipelineExecutor._stream_operational_outcome`` — the executor mapping that
  turns a runtime ``RouterNoMatchError`` into the terminal ``router_no_match``
  run status (distinct from ``failed``), backed by the ``TERMINAL_STATUSES``
  contract in ``modulo.db.models.run``.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from modulo.core.pipeline_engine.errors import RouterNoMatchError
from modulo.core.pipeline_engine.graph_cache import RouterConfigError, build_graph_from_json
from modulo.core.pipeline_engine.node_runner import make_router_node_fn

scenarios("../features/pipelines/router_nodes.feature")


@pytest.fixture
def ctx() -> dict[str, Any]:
    """Shared mutable context dict for the Router-node scenarios."""
    return {}


# ---------------------------------------------------------------------------
# Given — compile-path authoring
# ---------------------------------------------------------------------------


@given(parsers.parse('a pipeline graph with entry node "{entry}" and router node "{node_id}"'))
def _graph_with_router(ctx: dict[str, Any], entry: str, node_id: str) -> None:
    ctx["_graph_entry"] = entry
    ctx["_graph_router"] = node_id
    ctx["_router_rules"] = []


@given(parsers.parse('the router "{node_id}" declares a rule "{guard}" routing to "{target}"'))
def _router_declares_rule(ctx: dict[str, Any], node_id: str, guard: str, target: str) -> None:
    ctx["_router_rules"].append({"guard": guard, "target": target})


@given(parsers.parse('the router "{node_id}" declares a rule "{guard}" routing to "{target}" without a default'))
def _router_declares_rule_no_default(ctx: dict[str, Any], node_id: str, guard: str, target: str) -> None:
    """A new Router node with a guarded rule but NO explicit default rule."""
    ctx["_router_rules"].append({"guard": guard, "target": target})


@given(parsers.parse('the router "{node_id}" declares a default rule routing to "{target}"'))
def _router_declares_default(ctx: dict[str, Any], node_id: str, target: str) -> None:
    ctx["_router_rules"].append({"default": True, "target": target})


# ---------------------------------------------------------------------------
# Given — decision-function evaluation
# ---------------------------------------------------------------------------


@given(parsers.parse('a router node "{node_id}" with first-match-wins rules'))
def _router_eval_node(ctx: dict[str, Any], node_id: str) -> None:
    ctx["_router_rules"] = []
    ctx["_router_mode"] = None


@given(parsers.parse('the router rule "{guard}" routes to "{target}"'))
def _router_eval_rule(ctx: dict[str, Any], guard: str, target: str) -> None:
    ctx["_router_rules"].append({"guard": guard, "target": target})


@given(parsers.parse('the router has a default rule routing to "{target}"'))
def _router_eval_default(ctx: dict[str, Any], target: str) -> None:
    ctx["_router_rules"].append({"default": True, "target": target})


@given(parsers.parse('a classifier router node "{node_id}"'))
def _classifier_router_node(ctx: dict[str, Any], node_id: str) -> None:
    ctx["_router_rules"] = []
    ctx["_router_mode"] = "classifier"


@given(parsers.parse('the router rule label "{label}" routes to "{target}"'))
def _router_eval_label(ctx: dict[str, Any], label: str, target: str) -> None:
    ctx["_router_rules"].append({"label": label, "target": target})


# ---------------------------------------------------------------------------
# When — compile
# ---------------------------------------------------------------------------


def _router_config(ctx: dict[str, Any]) -> dict[str, Any]:
    config: dict[str, Any] = {"rules": ctx["_router_rules"]}
    if ctx.get("_router_mode"):
        config["mode"] = ctx["_router_mode"]
    return config


@when("the graph is compiled for execution")
def _compile_graph(ctx: dict[str, Any]) -> None:
    entry = ctx["_graph_entry"]
    router = ctx["_graph_router"]
    rules = ctx["_router_rules"]
    targets = sorted({str(rule.get("target")) for rule in rules if rule.get("target")})
    nodes = [{"id": entry, "node_type": "agent"}]
    nodes.extend({"id": target, "node_type": "agent"} for target in targets)
    nodes.append({"id": router, "node_type": "router", "router_config": _router_config(ctx)})
    edges = [{"source": entry, "target": router, "type": "normal"}]
    edges.extend({"source": router, "target": target, "type": "normal"} for target in targets)
    graph_json = {"nodes": nodes, "edges": edges}

    with (
        patch("modulo.core.pipeline_engine.graph_cache.make_node_fn", MagicMock()),
        patch("modulo.core.pipeline_engine.graph_cache.make_manual_node_fn", MagicMock()),
        patch("modulo.core.pipeline_engine.graph_cache.make_hitl_gate_fn", MagicMock()),
    ):
        try:
            ctx["_compiled"] = build_graph_from_json(graph_json)
            ctx["_compile_error"] = None
        except RouterConfigError as exc:
            ctx["_compile_error"] = exc
            ctx["_compiled"] = None


# ---------------------------------------------------------------------------
# When — decision-function evaluation
# ---------------------------------------------------------------------------


def _evaluate_router(ctx: dict[str, Any], state: dict[str, Any]) -> str | None:
    """Evaluate the real Router decision function against *state*."""
    router_fn = make_router_node_fn(_router_config(ctx), node_id="decider")
    try:
        ctx["_routing_error"] = None
        return router_fn(state)
    except RouterNoMatchError as exc:
        ctx["_routing_error"] = exc
        return None


@when(parsers.parse("the router evaluates state where severity is {severity:d}"))
def _evaluate_with_severity(ctx: dict[str, Any], severity: int) -> None:
    ctx["_routed_target"] = _evaluate_router(ctx, {"state": {"severity": severity}})


@when(parsers.parse('the router evaluates state where env is "{env}"'))
def _evaluate_with_env(ctx: dict[str, Any], env: str) -> None:
    ctx["_routed_target"] = _evaluate_router(ctx, {"state": {"env": env}})


@when(parsers.parse('the router evaluates state whose LLM-selected node is "{label}"'))
def _evaluate_with_llm_label(ctx: dict[str, Any], label: str) -> None:
    ctx["_routed_target"] = _evaluate_router(ctx, {"_llm_next_node": label})


@when("the router evaluates state with no LLM-selected node")
def _evaluate_without_llm_label(ctx: dict[str, Any]) -> None:
    ctx["_routed_target"] = _evaluate_router(ctx, {})


# ---------------------------------------------------------------------------
# Then — compile-path assertions
# ---------------------------------------------------------------------------


@then(parsers.parse('the graph compiles with the router node "{node_id}"'))
def _assert_graph_compiled_with_router(ctx: dict[str, Any], node_id: str) -> None:
    assert ctx.get("_compile_error") is None, f"graph compile failed: {ctx.get('_compile_error')!r}"
    compiled = ctx.get("_compiled")
    assert compiled is not None, "pipeline graph was not compiled"
    graph_nodes = compiled.get_graph().nodes
    assert node_id in graph_nodes, f"router node {node_id!r} missing from compiled graph: {sorted(graph_nodes)}"


@then(parsers.parse('router rule target "{target}" is not the pipeline entry point'))
def _assert_rule_target_not_entry(ctx: dict[str, Any], target: str) -> None:
    compiled = ctx["_compiled"]
    graph = compiled.get_graph()
    entry_nodes = [e.target for e in graph.edges if e.source == "__start__"]
    assert target not in entry_nodes, f"router rule target {target!r} became the pipeline entry point: {entry_nodes}"


@then("compiling refuses with RouterConfigError")
def _assert_compile_refused(ctx: dict[str, Any]) -> None:
    error = ctx.get("_compile_error")
    assert isinstance(error, RouterConfigError), f"expected RouterConfigError, got {error!r}"
    assert ctx.get("_compiled") is None, "graph compiled despite the invalid Router config"


# ---------------------------------------------------------------------------
# Then — decision-function assertions
# ---------------------------------------------------------------------------


@then(parsers.parse('the router routes the run to "{target}"'))
def _assert_routes_to(ctx: dict[str, Any], target: str) -> None:
    assert ctx.get("_routing_error") is None, f"routing refused with {ctx.get('_routing_error')!r}"
    assert ctx.get("_routed_target") == target, f"expected route to {target!r}, got {ctx.get('_routed_target')!r}"


@then(parsers.parse('the run does not route to "{first}" or "{second}"'))
def _assert_routes_neither(ctx: dict[str, Any], first: str, second: str) -> None:
    routed = ctx.get("_routed_target")
    assert routed != first, f"run unexpectedly routed to {first!r}"
    assert routed != second, f"run unexpectedly routed to {second!r}"


@then("routing refuses with RouterNoMatchError")
def _assert_refused_no_match(ctx: dict[str, Any]) -> None:
    error = ctx.get("_routing_error")
    assert isinstance(error, RouterNoMatchError), f"expected RouterNoMatchError, got {error!r}"


# ---------------------------------------------------------------------------
# Then — executor terminalization (RouterNoMatchError -> router_no_match)
# ---------------------------------------------------------------------------


@given("a running pipeline whose router node matches no rule and has no default")
def _running_pipeline_with_no_match(ctx: dict[str, Any]) -> None:
    ctx["_no_match_error"] = RouterNoMatchError(node_id="decider")


@when("the pipeline engine encounters the RouterNoMatchError")
def _engine_encounters_no_match(ctx: dict[str, Any]) -> None:
    from modulo.core.pipeline_engine.executor import PipelineExecutor

    broker = MagicMock()
    executor = PipelineExecutor(MagicMock())
    outcome = executor._stream_operational_outcome(
        ctx["_no_match_error"],
        broker,
        uuid.uuid4(),
        None,
    )
    ctx["_outcome"] = outcome


@then(parsers.parse('the run is terminalized with the status "{status}"'))
def _assert_terminal_status(ctx: dict[str, Any], status: str) -> None:
    outcome = ctx.get("_outcome")
    assert outcome is not None, "the pipeline engine produced no terminal outcome"
    assert outcome[0] == status, f"expected terminal status {status!r}, got {outcome[0]!r}"


@then(parsers.parse('the run error code is "{code}"'))
def _assert_terminal_error_code(ctx: dict[str, Any], code: str) -> None:
    outcome = ctx.get("_outcome")
    assert outcome is not None, "the pipeline engine produced no terminal outcome"
    assert outcome[1] == code, f"expected error code {code!r}, got {outcome[1]!r}"


@then('the run is not classified as "failed"')
def _assert_not_failed(ctx: dict[str, Any]) -> None:
    outcome = ctx.get("_outcome")
    assert outcome is not None, "the pipeline engine produced no terminal outcome"
    assert outcome[0] != "failed", f"router no-match must NOT classify the run as failed, got {outcome!r}"


@then(parsers.parse('"{status}" is a terminal run status'))
def _assert_terminal_run_status(ctx: dict[str, Any], status: str) -> None:
    from modulo.db.models.run import TERMINAL_STATUSES

    assert status in TERMINAL_STATUSES, f"{status!r} is not in TERMINAL_STATUSES: {sorted(TERMINAL_STATUSES)}"
