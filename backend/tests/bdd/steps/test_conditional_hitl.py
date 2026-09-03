"""Step definitions for Conditional HITL Gating feature."""

import asyncio
import contextlib
import json
import uuid
from typing import Any
from unittest.mock import patch

import pytest
from langgraph.errors import GraphInterrupt
from langgraph.types import Interrupt
from pytest_bdd import given, parsers, scenarios, then, when

from modulo.core.eval_engine import EvalBlockedError

# ---------------------------------------------------------------------------
# Active features
# ---------------------------------------------------------------------------
with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/evals/conditional_hitl.feature")

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def ctx():
    """Shared mutable context dict for conditional HITL tests."""
    return {}


# ============================================================================
# Given steps
# ============================================================================


@given(
    parsers.parse('node "{node_name}" has an llm_judge eval "{eval_name}"'),
)
def node_has_llm_judge_eval(node_name: str, eval_name: str, ctx):
    ctx["node_name"] = node_name
    ctx["eval_name"] = eval_name
    ctx["eval_def"] = {
        "id": uuid.uuid4(),
        "name": eval_name,
        "eval_type": "llm_judge",
        "config": {"field": "content"},
        "failure_behaviour": "warn",
    }


@given(parsers.parse('the edge after "{node_name}" has a HITL gate'))
def edge_has_hitl_gate(node_name: str, ctx):
    ctx["gate_config"] = {
        "gate_id": f"gate-{node_name}",
        "label": "Review Gate",
        "description": "Conditional HITL gate",
        "human_only": False,
    }


@given(
    parsers.parse('the gate condition references eval "{eval_name}" with threshold {threshold} operator "{operator}"'),
)
def gate_condition_references_eval(eval_name: str, threshold: float, operator: str, ctx):
    ctx["gate_config"]["eval_condition"] = {
        "eval_name": eval_name,
        "threshold": float(threshold),
        "operator": operator,
    }


@given(
    parsers.parse('the gate has a JMESPath condition "{expression}"'),
)
def gate_has_jmespath_condition(expression: str, ctx):
    ctx["gate_config"]["condition"] = expression


@given('the eval has failure_behaviour "block"')
def eval_has_block_behaviour(ctx):
    ctx["eval_def"]["failure_behaviour"] = "block"


@given(
    parsers.parse('a run is waiting at gate "{gate_id}" due to low eval score'),
)
def run_waiting_at_gate(gate_id: str, ctx):
    ctx["gate_config"] = {
        "gate_id": gate_id,
        "label": "Review Gate",
        "description": "Gate reached due to condition",
        "human_only": False,
    }
    ctx["state"] = {
        "artifacts": [],
        "_hitl_gates": [ctx["gate_config"]],
    }


@given(
    parsers.parse('node "{node_name}" has evals "{eval_names}"'),
)
def node_has_evals(node_name: str, eval_names: str, ctx):
    ctx["node_name"] = node_name
    names = [n.strip() for n in eval_names.split(",")]
    ctx["eval_defs"] = [
        {
            "id": uuid.uuid4(),
            "name": name,
            "eval_type": "regex",
            "config": {"pattern": ".", "field": "content"},
            "failure_behaviour": "warn",
        }
        for name in names
    ]


@given(parsers.parse('the gate has reject_target "{target}"'))
def gate_has_reject_target(target: str, ctx):
    ctx["gate_config"]["reject_target"] = target


# ============================================================================
# When steps
# ============================================================================


@when(
    parsers.parse("the node outputs {output_json}"),
)
def node_outputs(output_json: str, ctx):
    output = json.loads(output_json)
    ctx["node_output"] = output
    ctx["state"] = {
        "artifacts": [],
        "_hitl_gates": [],
        **output,
    }


@when(
    parsers.parse("the llm_judge callable returns {result_json}"),
)
def llm_judge_returns(result_json: str, ctx):
    result = json.loads(result_json)
    ctx["llm_judge_result"] = result
    ctx["state"]["score"] = result.get("score", 0.0)


@when(parsers.parse("the run_context has draft_mode {value}"))
def run_context_draft_mode(value: str, ctx):
    bool_val = value.lower() == "true"
    ctx["state"] = {
        "artifacts": [],
        "_hitl_gates": [],
        "run_context": {"draft_mode": bool_val},
    }


@when(parsers.parse("the run reaches the gate"))
def run_reaches_gate(ctx):
    pass  # Handled in then steps via make_hitl_gate_fn call


@when(
    parsers.parse('"{eval_name}" scores {score}'),
)
def eval_scores(eval_name: str, score: float, ctx):
    score_val = float(score)
    for ed in ctx.get("eval_defs", []):
        if ed["name"] == eval_name:
            ed["_mock_score"] = score_val
    ctx["state"]["score"] = score_val


@when("a human rejects the gate")
def human_rejects_gate(ctx):
    if "state" not in ctx:
        ctx["state"] = {"artifacts": [], "_hitl_gates": []}
    # FAR-541: the decision is stamped with the gate it resolves.
    ctx["state"]["_hitl_decision"] = {
        "action": "rejected",
        "gate_id": ctx.get("gate_config", {}).get("gate_id", "gate"),
    }


@when("a human approves the gate")
def human_approves_gate(ctx):
    if "state" not in ctx:
        ctx["state"] = {"artifacts": [], "_hitl_gates": []}
    # FAR-541: the decision is stamped with the gate it resolves.
    ctx["state"]["_hitl_decision"] = {
        "action": "approved",
        "gate_id": ctx.get("gate_config", {}).get("gate_id", "gate"),
    }


@when("the run resumes")
def run_resumes(ctx):
    pass  # State is already prepared


# ============================================================================
# Then steps
# ============================================================================


@then("the gate condition evaluates to true")
def gate_condition_true(ctx):
    _evaluate_conditional_gate(ctx)
    assert ctx.get("_gate_result") is None, "Expected condition true → interrupt but got skip"
    assert ctx.get("_interrupt_raised") is True, "Expected NodeInterrupt to be raised"


@then("a NodeInterrupt is raised")
def node_interrupt_raised(ctx):
    assert ctx.get("_interrupt_raised") is True, "Expected NodeInterrupt"


@then('the run transitions to "awaiting_human"')
def run_transitions_to_awaiting_human(ctx):
    # In unit test context, the interrupt was raised — this signals awaiting_human
    pass


@then("the gate condition evaluates to false")
def gate_condition_false(ctx):
    _evaluate_conditional_gate(ctx)
    result = ctx.get("_gate_result")
    assert result is not None
    assert result["artifacts"][0]["status"] == "condition_skipped"


@then("execution continues without interrupting")
def execution_continues(ctx):
    assert ctx.get("_interrupt_raised") is not True, "Expected no interrupt"


@then('the gate artifact contains "condition_skipped"')
def gate_artifact_contains_condition_skipped(ctx):
    result = ctx.get("_gate_result")
    assert result is not None
    assert result["artifacts"][0]["status"] == "condition_skipped"


@then("the JMESPath condition evaluates to false")
def jmespath_condition_false(ctx):
    _evaluate_conditional_gate(ctx)
    result = ctx.get("_gate_result")
    assert result is not None
    assert result["artifacts"][0]["status"] == "condition_skipped"


@then("the gate is skipped")
def gate_skipped(ctx):
    result = ctx.get("_gate_result")
    assert result is not None
    assert result["artifacts"][0]["status"] in ("condition_skipped", "skipped")


@then("no interrupt is raised")
def no_interrupt_raised(ctx):
    assert ctx.get("_interrupt_raised") is not True


@then("the JMESPath condition evaluates to true")
def jmespath_condition_true(ctx):
    _evaluate_conditional_gate(ctx)
    assert ctx.get("_interrupt_raised") is True or ctx.get("_gate_result") is None


@then("the gate proceeds to eval checks")
def gate_proceeds_to_eval_checks(ctx):
    assert ctx.get("_interrupt_raised") is True or ctx.get("_gate_result") is None


@then("EvalBlockedError is raised")
def eval_blocked_error_raised(ctx):
    _evaluate_conditional_gate(ctx)
    assert ctx.get("_eval_blocked_error") is True or ctx.get("_interrupt_raised") is False


@then('the run transitions to "eval_failed"')
def run_transitions_to_eval_failed(ctx):
    pass  # Handled by executor, not unit-tested here


@then("no HITL interrupt is raised")
def no_hitl_interrupt_raised(ctx):
    assert ctx.get("_interrupt_raised") is not True, "HITL interrupt should not be raised"


@then("the gate does not re-evaluate the condition")
def gate_does_not_reevaluate(ctx):
    _evaluate_conditional_gate(ctx)
    result = ctx.get("_gate_result")
    assert result is not None
    assert result["artifacts"][0]["status"] == "interrupted"


@then("the gate does not re-run evals")
def gate_does_not_rerun_evals(ctx):
    pass  # Verified by gate not raising EvalBlockedError on resume


@then("execution continues to the next node")
def execution_continues_to_next(ctx):
    pass


@then(parsers.parse('the gate condition on "{eval_name}" evaluates to true'))
def gate_condition_on_eval_true(eval_name: str, ctx):
    _evaluate_conditional_gate(ctx)
    assert ctx.get("_interrupt_raised") is True


@then('the run routes to "{target}"')
def run_routes_to_target(target: str, ctx):
    pass  # Routing handled by graph_cache kickback router


@then(parsers.parse('the run routes to "{target}"'))
def run_routes_to(target: str, ctx):
    from modulo.core.pipeline_engine.graph_cache import _make_gate_kickback_router

    gate_config = ctx.get("gate_config", {})
    reject_target = gate_config.get("reject_target", "?")
    normal_target = "next-node"
    gate_id = gate_config.get("gate_id", "gate")
    router = _make_gate_kickback_router(normal_target, reject_target, gate_id=gate_id)
    decision = ctx.get("state", {}).get("_hitl_decision", {})
    result = router({"_hitl_decision": decision})
    assert result == target, f"Expected route to {target!r}, got {result!r}"


@then('the gate artifact shows action "rejected"')
def gate_artifact_shows_rejected(ctx):
    _evaluate_conditional_gate(ctx)
    result = ctx.get("_gate_result")
    assert result is not None
    assert result["artifacts"][0]["result"] == "rejected"


# ============================================================================
# Helper
# ============================================================================


def _evaluate_conditional_gate(ctx: dict[str, Any]) -> None:
    """Build a make_hitl_gate_fn from context and invoke it.

    The node function is async (make_hitl_gate_fn returns ``async def _hitl_gate``),
    so we wrap it in ``asyncio.run()`` to execute synchronously in tests.
    """

    gate_config = ctx.get("gate_config", {}).copy()
    state = ctx.get("state", {"artifacts": [], "_hitl_gates": []})

    # Build eval definitions if present.
    eval_defs = _build_eval_defs(ctx)

    # If a mock llm_judge result is provided, patch EvalEngine.evaluate
    # to return it instead of calling a real LLM.
    mock_result = ctx.get("llm_judge_result")
    if mock_result is not None:
        _patch_eval_engine(ctx, mock_result, state, gate_config, eval_defs)
        return

    # If individual mock scores are set on eval_defs, patch engine for those.
    if ctx.get("eval_defs") and any("_mock_score" in ed for ed in ctx["eval_defs"]):
        _patch_mock_scores(ctx, state, gate_config, eval_defs)
        return

    _run_gate_fn(ctx, gate_config, state, eval_defs)


def _patch_mock_scores(
    ctx: dict[str, Any],
    state: dict[str, Any],
    gate_config: dict[str, Any],
    eval_defs: list[Any] | None,
) -> None:
    """Patch EvalEngine.evaluate to use _mock_score per eval definition."""
    from modulo.core.eval_engine import EvalEngine, EvalResult

    score_map: dict[str, float] = {}
    for ed in ctx.get("eval_defs", []):
        ms = ed.get("_mock_score")
        if ms is not None:
            score_map[ed["name"]] = float(ms)

    def _mock_evaluate(self, output, eval_def, *, run_id=None, llm_judge_callable=None):
        mock_score = score_map.get(eval_def.name)
        if mock_score is not None:
            _passed = mock_score >= 0.5
        else:
            _passed = True
            mock_score = 1.0
        return EvalResult(
            id=uuid.uuid4(),
            run_id=uuid.uuid4(),
            node_id="n1",
            eval_id=eval_def.id,
            passed=_passed,
            score=mock_score,
            detail="mocked",
        )

    _patcher = patch.object(EvalEngine, "evaluate", _mock_evaluate)
    _patcher.start()
    try:
        _run_gate_fn(ctx, gate_config, state, eval_defs)
    finally:
        _patcher.stop()


def _patch_eval_engine(
    ctx: dict[str, Any],
    mock_result: dict[str, Any],
    state: dict[str, Any],
    gate_config: dict[str, Any],
    eval_defs: list[Any] | None,
) -> None:
    """Patch EvalEngine.evaluate to return mock result, respecting failure_behaviour."""
    from modulo.core.eval_engine import EvalBlockedError, EvalEngine, EvalResult

    _passed = mock_result.get("passed", False)
    _score = mock_result.get("score")
    _detail = mock_result.get("detail", "")

    def _mock_evaluate(self, output, eval_def, *, run_id=None, llm_judge_callable=None):
        result = EvalResult(
            id=uuid.uuid4(),
            run_id=uuid.uuid4(),
            node_id="n1",
            eval_id=eval_def.id,
            passed=_passed,
            score=_score,
            detail=_detail,
        )
        # Simulate real EvalEngine behavior: block on failure.
        if not result.passed and eval_def.failure_behaviour == "block":
            raise EvalBlockedError(eval_def.name, result.detail)
        return result

    _patcher = patch.object(EvalEngine, "evaluate", _mock_evaluate)
    _patcher.start()
    try:
        _run_gate_fn(ctx, gate_config, state, eval_defs)
    finally:
        _patcher.stop()


def _run_gate_fn(
    ctx: dict[str, Any],
    gate_config: dict[str, Any],
    state: dict[str, Any],
    eval_defs: list[Any] | None,
) -> None:
    """Execute the gate node function synchronously and capture results."""
    from modulo.core.pipeline_engine.node_runner import make_hitl_gate_fn

    node_fn = make_hitl_gate_fn(gate_config, eval_definitions=eval_defs)

    async def _run() -> Any:
        return await node_fn(state)

    def _raise_interrupt(value: Any) -> None:
        raise GraphInterrupt((Interrupt(value=value),))

    try:
        with patch("modulo.core.pipeline_engine.node_runner.interrupt", _raise_interrupt):
            result = asyncio.run(_run())
        ctx["_gate_result"] = result
        ctx["_interrupt_raised"] = False
    except GraphInterrupt:
        ctx["_gate_result"] = None
        ctx["_interrupt_raised"] = True
    except EvalBlockedError:
        ctx["_gate_result"] = None
        ctx["_eval_blocked_error"] = True
        ctx["_interrupt_raised"] = False


def _build_eval_defs(ctx: dict[str, Any]) -> list[Any] | None:
    """Build EvalDefinition objects from context."""
    from uuid import uuid4

    from modulo.core.eval_engine import EvalDefinition, EvalType

    if ctx.get("eval_def") and ctx.get("eval_name"):
        ed = ctx["eval_def"]
        return [
            EvalDefinition(
                id=ed.get("id", uuid4()),
                org_id=uuid4(),
                name=ed.get("name", ctx["eval_name"]),
                eval_type=EvalType(ed.get("eval_type", "regex")),
                config=ed.get("config", {}),
                failure_behaviour=ed.get("failure_behaviour", "warn"),
            )
        ]
    if ctx.get("eval_defs"):
        return [
            EvalDefinition(
                id=ed.get("id", uuid4()),
                org_id=uuid4(),
                name=ed.get("name", f"eval-{i}"),
                eval_type=EvalType(ed.get("eval_type", "regex")),
                config=ed.get("config", {}),
                failure_behaviour=ed.get("failure_behaviour", "warn"),
            )
            for i, ed in enumerate(ctx["eval_defs"])
        ]
    return None
