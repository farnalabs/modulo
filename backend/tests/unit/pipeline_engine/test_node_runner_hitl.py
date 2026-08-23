"""Unit tests for HITL gate and manual node functions."""

from contextlib import asynccontextmanager
from typing import Any, Self
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from langgraph.errors import GraphInterrupt

from modulo.core.eval_engine import EvalBlockedError, EvalDefinition, EvalType
from modulo.core.pipeline_engine.node_runner import (
    _build_hitl_gate_artifact,
    _evaluate_eval_condition,
    _hitl_gate_autonomy_result,
    _hitl_gate_condition_skip,
    make_hitl_gate_fn,
    make_manual_node_fn,
)


@pytest.fixture(autouse=True)
def _interrupt_without_graph_runtime_autouse(_interrupt_without_graph_runtime: None) -> None:
    """Apply the shared Interrupt()-shim fixture to every test in this module."""

    assert _interrupt_without_graph_runtime is None


# ---------------------------------------------------------------------------
# HITL gate node — first invocation (raises GraphInterrupt)
# ---------------------------------------------------------------------------


async def test_hitl_gate_first_call_raises_interrupt():
    gate_config = {"gate_id": "review-step", "human_only": False}
    node_fn = make_hitl_gate_fn(gate_config)

    with pytest.raises(GraphInterrupt) as exc_info:
        await node_fn({"artifacts": [], "_hitl_gates": []})

    # GraphInterrupt(value) stores value in args as [Interrupt(value, ...)].
    interrupt_list = exc_info.value.args[0]
    assert len(interrupt_list) > 0
    actual = interrupt_list[0]
    value = actual.value if hasattr(actual, "value") else actual
    assert isinstance(value, dict)
    assert value["gate_id"] == "review-step"


async def test_hitl_gate_first_call_stores_gate_config_in_state():
    gate_config = {"gate_id": "review-step"}
    node_fn = make_hitl_gate_fn(gate_config)

    state: dict[str, Any] = {"artifacts": [], "_hitl_gates": []}
    with pytest.raises(GraphInterrupt):
        await node_fn(state)

    # State mutations before the raise should be persisted.
    assert len(state["_hitl_gates"]) == 1
    assert state["_hitl_gates"][0]["gate_id"] == "review-step"


async def test_hitl_gate_first_call_preserves_existing_hitl_gates():
    gate_config = {"gate_id": "second-gate"}
    node_fn = make_hitl_gate_fn(gate_config)

    state: dict[str, Any] = {
        "artifacts": [],
        "_hitl_gates": [{"gate_id": "first-gate"}],
    }
    with pytest.raises(GraphInterrupt):
        await node_fn(state)

    assert len(state["_hitl_gates"]) == 2


# ---------------------------------------------------------------------------
# HITL gate node — resume (state has _hitl_decision)
# ---------------------------------------------------------------------------


async def test_hitl_gate_accepts_command_resume_value(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "modulo.core.pipeline_engine.node_runner.interrupt",
        lambda _payload: {"action": "approved", "notes": "Command resume"},
    )
    node_fn = make_hitl_gate_fn({"gate_id": "review-step"})

    result = await node_fn({"artifacts": [], "_hitl_gates": []})

    assert result["artifacts"][0]["result"] == "approved"
    assert result["artifacts"][0]["human_data"]["notes"] == "Command resume"


async def test_hitl_gate_resume_with_approved():
    gate_config = {"gate_id": "review-step"}
    node_fn = make_hitl_gate_fn(gate_config)

    result = await node_fn(
        {
            "artifacts": [],
            "_hitl_decision": {"action": "approved", "notes": "Looks good"},
        }
    )

    assert len(result["artifacts"]) == 1
    assert result["artifacts"][0]["result"] == "approved"
    assert result["artifacts"][0]["human_data"] == {"action": "approved", "notes": "Looks good"}


async def test_hitl_gate_resume_with_rejected():
    gate_config = {"gate_id": "review-step"}
    node_fn = make_hitl_gate_fn(gate_config)

    result = await node_fn(
        {
            "artifacts": [],
            "_hitl_decision": {"action": "rejected", "reason": "Not good enough"},
        }
    )

    assert result["artifacts"][0]["result"] == "rejected"


async def test_hitl_gate_resume_preserves_existing_artifacts():
    """Gate returns delta artifacts on resume; accumulator handles merge."""
    gate_config = {"gate_id": "review-step"}
    prior_artifact = {"node_id": "prior-node", "status": "executed"}
    node_fn = make_hitl_gate_fn(gate_config)

    result = await node_fn(
        {
            "artifacts": [prior_artifact],
            "_hitl_decision": {"action": "approved"},
        }
    )

    assert len(result["artifacts"]) == 1
    assert result["artifacts"][0]["node_id"] == "review-step"
    assert result["artifacts"][0]["result"] == "approved"


# ---------------------------------------------------------------------------
# Manual node — first invocation (raises GraphInterrupt)
# ---------------------------------------------------------------------------


async def test_manual_node_first_call_raises_interrupt():
    node_def = {"id": "manual-node-1", "node_type": "manual"}
    node_fn = make_manual_node_fn(node_def)

    with pytest.raises(GraphInterrupt) as exc_info:
        await node_fn({"artifacts": [], "_hitl_gates": []})

    interrupt_list = exc_info.value.args[0]
    assert len(interrupt_list) > 0
    actual = interrupt_list[0]
    value = actual.value if hasattr(actual, "value") else actual
    assert isinstance(value, dict)
    assert value["manual"] is True
    assert value["node_id"] == "manual-node-1"


# ---------------------------------------------------------------------------
# Manual node — resume (state has _hitl_decision with output)
# ---------------------------------------------------------------------------


async def test_manual_node_accepts_command_resume_value(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "modulo.core.pipeline_engine.node_runner.interrupt",
        lambda _payload: {"output": {"answer": "provided"}},
    )
    node_fn = make_manual_node_fn({"id": "manual-step", "manual_prompt": "Provide output"})

    result = await node_fn({"artifacts": []})

    assert result["manual_output"] == {"answer": "provided"}


async def test_manual_node_resume_with_output():
    node_def = {"id": "manual-node-1", "node_type": "manual"}
    node_fn = make_manual_node_fn(node_def)

    result = await node_fn(
        {
            "artifacts": [],
            "_hitl_decision": {"action": "manual_output", "output": {"title": "Test"}},
        }
    )

    assert result["manual_output"] == {"title": "Test"}


async def test_manual_node_resume_validates_required_fields():
    node_def = {
        "id": "manual-node-2",
        "node_type": "manual",
        "output_schema_json": {"required": ["title", "body"]},
    }
    node_fn = make_manual_node_fn(node_def)

    with pytest.raises(ValueError, match="missing required field"):
        await node_fn(
            {
                "artifacts": [],
                "_hitl_decision": {"action": "manual_output", "output": {"title": "Only title"}},
            }
        )


async def test_manual_node_resume_without_schema_passes_any_data():
    node_def = {"id": "manual-node-3", "node_type": "manual"}
    node_fn = make_manual_node_fn(node_def)

    result = await node_fn(
        {
            "artifacts": [],
            "_hitl_decision": {"action": "manual_output", "output": {"anything": 42}},
        }
    )

    assert result["manual_output"] == {"anything": 42}


# ---------------------------------------------------------------------------
# HITL gate node — autonomy level integration
# ---------------------------------------------------------------------------


async def test_hitl_gate_fully_autonomous_skips_gate():
    gate_config = {"gate_id": "auto-gate", "human_only": False}
    node_fn = make_hitl_gate_fn(gate_config)

    result = await node_fn(
        {
            "artifacts": [],
            "run_context": {
                "_pipeline_default_autonomy": "fully_autonomous",
            },
        }
    )

    assert len(result["artifacts"]) == 1
    assert result["artifacts"][0]["status"] == "skipped"
    assert result["artifacts"][0]["autonomy"] == "fully_autonomous"


async def test_hitl_gate_notify_on_complete_auto_approves():
    gate_config = {"gate_id": "notify-gate", "human_only": False}
    node_fn = make_hitl_gate_fn(gate_config)

    result = await node_fn(
        {
            "artifacts": [],
            "run_context": {
                "_pipeline_default_autonomy": "notify_on_complete",
            },
        }
    )

    assert len(result["artifacts"]) == 1
    assert result["artifacts"][0]["status"] == "auto_approved"
    assert result["artifacts"][0]["autonomy"] == "notify_on_complete"


async def test_hitl_gate_run_context_recommendation_overrides_pipeline_default():
    gate_config = {"gate_id": "rec-gate", "human_only": False}
    node_fn = make_hitl_gate_fn(gate_config)

    result = await node_fn(
        {
            "artifacts": [],
            "run_context": {
                "_pipeline_default_autonomy": "manual_approval",
                "autonomy_recommendation": "fully_autonomous",
            },
        }
    )

    assert result["artifacts"][0]["status"] == "skipped"
    assert result["artifacts"][0]["autonomy"] == "fully_autonomous"


async def test_hitl_gate_human_only_overrides_fully_autonomous():
    gate_config = {"gate_id": "human-override", "human_only": True}
    node_fn = make_hitl_gate_fn(gate_config)

    with pytest.raises(GraphInterrupt) as exc_info:
        await node_fn(
            {
                "artifacts": [],
                "_hitl_gates": [],
                "run_context": {
                    "_pipeline_default_autonomy": "fully_autonomous",
                },
            }
        )

    interrupt_list = exc_info.value.args[0]
    assert interrupt_list[0].value["gate_id"] == "human-override"
    assert interrupt_list[0].value["autonomy_level"] == "fully_autonomous"
    assert interrupt_list[0].value["human_only"] is True


async def test_hitl_gate_manual_approval_raises_interrupt():
    gate_config = {"gate_id": "manual-gate", "human_only": False}
    node_fn = make_hitl_gate_fn(gate_config)

    with pytest.raises(GraphInterrupt) as exc_info:
        await node_fn(
            {
                "artifacts": [],
                "_hitl_gates": [],
                "run_context": {
                    "_pipeline_default_autonomy": "manual_approval",
                },
            }
        )

    interrupt_value = exc_info.value.args[0][0].value
    assert interrupt_value["gate_id"] == "manual-gate"
    assert interrupt_value["autonomy_level"] == "manual_approval"
    assert interrupt_value["human_only"] is False


async def test_hitl_gate_no_run_context_falls_back_to_manual_approval():
    gate_config = {"gate_id": "no-ctx-gate", "human_only": False}
    node_fn = make_hitl_gate_fn(gate_config)

    with pytest.raises(GraphInterrupt):
        await node_fn(
            {
                "artifacts": [],
                "_hitl_gates": [],
            }
        )

    # No run_context at all = safe fallback to manual_approval → interrupt raised.


async def test_hitl_gate_skipped_does_not_record_hitl_gate_state():
    gate_config = {"gate_id": "skip-gate", "human_only": False}
    node_fn = make_hitl_gate_fn(gate_config)

    state: dict[str, Any] = {
        "artifacts": [],
        "_hitl_gates": [],
        "run_context": {"_pipeline_default_autonomy": "fully_autonomous"},
    }
    result = await node_fn(state)

    # The gate was skipped, so _hitl_gates should NOT have been mutated.
    assert not state.get("_hitl_gates", [])
    assert result["artifacts"][0]["status"] == "skipped"


async def test_hitl_gate_notify_on_complete_preserves_artifacts():
    prior_artifact = {"node_id": "prior", "status": "executed"}
    gate_config = {"gate_id": "notify-preserve", "human_only": False}
    node_fn = make_hitl_gate_fn(gate_config)

    result = await node_fn(
        {
            "artifacts": [prior_artifact],
            "run_context": {"_pipeline_default_autonomy": "notify_on_complete"},
        }
    )

    assert len(result["artifacts"]) == 1
    assert result["artifacts"][0]["node_id"] == "notify-preserve"
    assert result["artifacts"][0]["status"] == "auto_approved"


# ---------------------------------------------------------------------------
# Conditional gate — condition JMESPath expression (§8.17)
# ---------------------------------------------------------------------------


async def test_condition_truthy_proceeds_to_interrupt():
    """Condition returns a truthy value → gate normal interrupt."""
    gate_config = {"gate_id": "cond-gate", "condition": "score > `0.5`"}
    node_fn = make_hitl_gate_fn(gate_config)

    with pytest.raises(GraphInterrupt) as exc_info:
        await node_fn(
            {
                "artifacts": [],
                "_hitl_gates": [],
                "score": 0.8,
            }
        )

    interrupt_list = exc_info.value.args[0]
    assert interrupt_list[0].value["gate_id"] == "cond-gate"


async def test_condition_falsy_skips_gate():
    """Condition returns a falsy value → gate is skipped, no interrupt."""
    gate_config = {"gate_id": "cond-gate", "condition": "score > `0.5`"}
    node_fn = make_hitl_gate_fn(gate_config)

    result = await node_fn(
        {
            "artifacts": [],
            "score": 0.2,
        }
    )

    assert len(result["artifacts"]) == 1
    assert result["artifacts"][0]["status"] == "condition_skipped"
    assert result["artifacts"][0]["condition"] == "score > `0.5`"


async def test_condition_empty_string_is_falsy():
    """Condition returns an empty string → gate is skipped."""
    gate_config = {"gate_id": "cond-gate", "condition": "msg"}
    node_fn = make_hitl_gate_fn(gate_config)

    result = await node_fn(
        {
            "artifacts": [],
            "msg": "",
        }
    )

    assert result["artifacts"][0]["status"] == "condition_skipped"


async def test_condition_none_is_falsy():
    """Condition returns null → gate is skipped."""
    gate_config = {"gate_id": "cond-gate", "condition": "missing"}
    node_fn = make_hitl_gate_fn(gate_config)

    result = await node_fn(
        {
            "artifacts": [],
            "msg": "hello",
        }
    )

    assert result["artifacts"][0]["status"] == "condition_skipped"


async def test_condition_absent_defaults_to_interrupt():
    """No condition field → normal interrupt (backward compatible)."""
    gate_config = {"gate_id": "no-cond-gate", "human_only": False}
    node_fn = make_hitl_gate_fn(gate_config)

    with pytest.raises(GraphInterrupt):
        await node_fn(
            {
                "artifacts": [],
                "_hitl_gates": [],
            }
        )


async def test_condition_zero_number_is_falsy():
    """Condition returns number 0 → gate is skipped."""
    gate_config = {"gate_id": "cond-gate", "condition": "count"}
    node_fn = make_hitl_gate_fn(gate_config)

    result = await node_fn(
        {
            "artifacts": [],
            "count": 0,
        }
    )

    assert result["artifacts"][0]["status"] == "condition_skipped"


async def test_condition_nonzero_number_is_truthy():
    """Condition returns a non-zero number → interrupt proceeds."""
    gate_config = {"gate_id": "cond-gate", "condition": "count"}
    node_fn = make_hitl_gate_fn(gate_config)

    with pytest.raises(GraphInterrupt):
        await node_fn(
            {
                "artifacts": [],
                "_hitl_gates": [],
                "count": 42,
            }
        )


async def test_condition_true_bool_is_truthy():
    """Condition returns true boolean → interrupt proceeds."""
    gate_config = {"gate_id": "cond-gate", "condition": "ready == `true`"}
    node_fn = make_hitl_gate_fn(gate_config)

    with pytest.raises(GraphInterrupt):
        await node_fn(
            {
                "artifacts": [],
                "_hitl_gates": [],
                "ready": True,
            }
        )


# ---------------------------------------------------------------------------
# Eval-before-interrupt (§8.17)
# ---------------------------------------------------------------------------


async def test_eval_block_fails_raises_eval_blocked_error():
    """Eval with failure_behaviour='block' that fails → EvalBlockedError."""
    gate_config = {"gate_id": "eval-gate"}
    eval_def = EvalDefinition(
        id=uuid4(),
        org_id=uuid4(),
        name="check_score",
        eval_type=EvalType.REGEX,
        config={"pattern": "high", "field": "level"},
        failure_behaviour="block",
    )
    node_fn = make_hitl_gate_fn(gate_config, eval_definitions=[eval_def])

    with pytest.raises(EvalBlockedError, match="check_score"):
        await node_fn(
            {
                "artifacts": [],
                "_hitl_gates": [],
                "level": "low",
            }
        )


async def test_eval_warn_fails_still_interrupts():
    """Eval with failure_behaviour='warn' that fails → interrupt still occurs."""
    gate_config = {"gate_id": "eval-gate"}
    eval_def = EvalDefinition(
        id=uuid4(),
        org_id=uuid4(),
        name="check_score",
        eval_type=EvalType.REGEX,
        config={"pattern": "high", "field": "level"},
        failure_behaviour="warn",
    )
    node_fn = make_hitl_gate_fn(gate_config, eval_definitions=[eval_def])

    with pytest.raises(GraphInterrupt):
        await node_fn(
            {
                "artifacts": [],
                "_hitl_gates": [],
                "level": "low",
            }
        )


async def test_eval_all_pass_proceeds_to_interrupt():
    """All evals pass → interrupt occurs normally."""
    gate_config = {"gate_id": "eval-gate"}
    eval_def = EvalDefinition(
        id=uuid4(),
        org_id=uuid4(),
        name="check_score",
        eval_type=EvalType.REGEX,
        config={"pattern": "high", "field": "level"},
        failure_behaviour="block",
    )
    node_fn = make_hitl_gate_fn(gate_config, eval_definitions=[eval_def])

    with pytest.raises(GraphInterrupt):
        await node_fn(
            {
                "artifacts": [],
                "_hitl_gates": [],
                "level": "high",
            }
        )


async def test_no_eval_definitions_proceeds_to_interrupt():
    """No eval_definitions passed → normal interrupt."""
    gate_config = {"gate_id": "eval-gate"}
    node_fn = make_hitl_gate_fn(gate_config, eval_definitions=None)

    with pytest.raises(GraphInterrupt):
        await node_fn(
            {
                "artifacts": [],
                "_hitl_gates": [],
            }
        )


async def test_empty_eval_definitions_proceeds_to_interrupt():
    """Empty eval_definitions list → normal interrupt."""
    gate_config = {"gate_id": "eval-gate"}
    node_fn = make_hitl_gate_fn(gate_config, eval_definitions=[])

    with pytest.raises(GraphInterrupt):
        await node_fn(
            {
                "artifacts": [],
                "_hitl_gates": [],
            }
        )


# ---------------------------------------------------------------------------
# Eval-reference condition (§8.17 v1)
# ---------------------------------------------------------------------------


async def test_eval_condition_below_threshold_triggers_interrupt():
    """Eval-reference condition: score < threshold (operator lt) → interrupt."""
    gate_config = {
        "gate_id": "eval-cond-gate",
        "eval_condition": {"eval_name": "quality-check", "threshold": 0.8, "operator": "lt"},
    }
    eval_def = EvalDefinition(
        id=uuid4(),
        org_id=uuid4(),
        name="quality-check",
        eval_type=EvalType.REGEX,
        config={"pattern": "pass", "field": "level"},
        failure_behaviour="warn",
    )
    node_fn = make_hitl_gate_fn(gate_config, eval_definitions=[eval_def])

    with pytest.raises(GraphInterrupt) as exc_info:
        await node_fn(
            {
                "artifacts": [],
                "_hitl_gates": [],
                "level": "fail",
            }
        )

    interrupt_list = exc_info.value.args[0]
    assert interrupt_list[0].value["gate_id"] == "eval-cond-gate"


async def test_eval_condition_at_threshold_skips_gate():
    """Eval-reference condition: score >= threshold (operator lt) → gate skipped."""
    gate_config = {
        "gate_id": "eval-cond-gate",
        "eval_condition": {"eval_name": "quality-check", "threshold": 0.8, "operator": "lt"},
    }
    eval_def = EvalDefinition(
        id=uuid4(),
        org_id=uuid4(),
        name="quality-check",
        eval_type=EvalType.REGEX,
        config={"pattern": "pass", "field": "level"},
        failure_behaviour="warn",
    )
    node_fn = make_hitl_gate_fn(gate_config, eval_definitions=[eval_def])

    result = await node_fn(
        {
            "artifacts": [],
            "level": "pass",
        }
    )

    assert result["artifacts"][0]["status"] == "condition_skipped"
    assert result["artifacts"][0]["condition_result"] is False


async def test_eval_condition_with_gt_operator():
    """Eval-reference condition with gt: score > threshold → gate fires."""
    gate_config = {
        "gate_id": "eval-cond-gt",
        "eval_condition": {"eval_name": "anomaly-check", "threshold": 0.5, "operator": "gt"},
    }
    eval_def = EvalDefinition(
        id=uuid4(),
        org_id=uuid4(),
        name="anomaly-check",
        eval_type=EvalType.REGEX,
        config={"pattern": "anomaly", "field": "level"},
        failure_behaviour="warn",
    )
    node_fn = make_hitl_gate_fn(gate_config, eval_definitions=[eval_def])

    with pytest.raises(GraphInterrupt):
        await node_fn(
            {
                "artifacts": [],
                "_hitl_gates": [],
                "level": "anomaly detected",
            }
        )


async def test_eval_condition_no_eval_definition_skips_check():
    """No eval definitions means eval_condition has nothing to check — gate passes through."""
    gate_config = {
        "gate_id": "eval-cond-absent",
        "eval_condition": {"eval_name": "missing-eval", "threshold": 0.8, "operator": "lt"},
    }
    node_fn = make_hitl_gate_fn(gate_config, eval_definitions=[])

    with pytest.raises(GraphInterrupt):
        await node_fn(
            {
                "artifacts": [],
                "_hitl_gates": [],
            }
        )


async def test_eval_condition_none_skips_check():
    """eval_condition absent → no condition check, normal interrupt."""
    gate_config = {"gate_id": "no-eval-cond"}
    node_fn = make_hitl_gate_fn(gate_config)

    with pytest.raises(GraphInterrupt):
        await node_fn(
            {
                "artifacts": [],
                "_hitl_gates": [],
            }
        )


async def test_eval_condition_score_equal_threshold_with_eq():
    """eval_condition with operator eq: score == threshold → gate fires."""
    gate_config = {
        "gate_id": "eval-cond-eq",
        "eval_condition": {"eval_name": "exact-check", "threshold": 1.0, "operator": "eq"},
    }
    eval_def = EvalDefinition(
        id=uuid4(),
        org_id=uuid4(),
        name="exact-check",
        eval_type=EvalType.REGEX,
        config={"pattern": "perfect", "field": "level"},
        failure_behaviour="warn",
    )
    node_fn = make_hitl_gate_fn(gate_config, eval_definitions=[eval_def])

    with pytest.raises(GraphInterrupt):
        await node_fn(
            {
                "artifacts": [],
                "_hitl_gates": [],
                "level": "perfect",
            }
        )


async def test_eval_condition_resume_skips_condition_and_eval():
    """On resume with _hitl_decision, eval_condition is not checked."""
    gate_config = {
        "gate_id": "resume-eval-cond",
        "eval_condition": {"eval_name": "quality", "threshold": 0.8, "operator": "lt"},
    }
    eval_def = EvalDefinition(
        id=uuid4(),
        org_id=uuid4(),
        name="quality",
        eval_type=EvalType.REGEX,
        config={"pattern": "pass", "field": "x"},
        failure_behaviour="block",
    )
    node_fn = make_hitl_gate_fn(gate_config, eval_definitions=[eval_def])

    result = await node_fn(
        {
            "artifacts": [],
            "x": "fail",
            "_hitl_decision": {"action": "approved"},
        }
    )

    assert result["artifacts"][0]["result"] == "approved"
    # No EvalBlockedError means evals were skipped due to resume priority.


# ---------------------------------------------------------------------------
# Condition + eval interaction
# ---------------------------------------------------------------------------


async def test_condition_falsy_skips_eval_and_gate():
    """Condition falsy → gate skipped, evals NOT run (no EvalBlockedError)."""
    gate_config = {"gate_id": "cond-eval-gate", "condition": "score > `0.5`"}
    eval_def = EvalDefinition(
        id=uuid4(),
        org_id=uuid4(),
        name="check_score",
        eval_type=EvalType.REGEX,
        config={"pattern": "pass", "field": "level"},
        failure_behaviour="block",
    )
    node_fn = make_hitl_gate_fn(gate_config, eval_definitions=[eval_def])

    # Condition is falsy (score=0.2), so evals are not run and no interrupt.
    result = await node_fn(
        {
            "artifacts": [],
            "score": 0.2,
            "level": "fail",
        }
    )

    assert result["artifacts"][0]["status"] == "condition_skipped"


async def test_resume_skips_condition_and_eval():
    """On resume, _hitl_decision is checked before condition/evals."""
    gate_config = {"gate_id": "resume-gate", "condition": "score > `0.5`"}
    eval_def = EvalDefinition(
        id=uuid4(),
        org_id=uuid4(),
        name="check_score",
        eval_type=EvalType.REGEX,
        config={"pattern": "pass", "field": "level"},
        failure_behaviour="block",
    )
    node_fn = make_hitl_gate_fn(gate_config, eval_definitions=[eval_def])

    # Resume with _hitl_decision present — condition and evals are skipped.
    result = await node_fn(
        {
            "artifacts": [],
            "score": 0.2,
            "level": "fail",
            "_hitl_decision": {"action": "approved"},
        }
    )

    assert len(result["artifacts"]) == 1
    assert result["artifacts"][0]["status"] == "interrupted"
    assert result["artifacts"][0]["result"] == "approved"


# ---------------------------------------------------------------------------
# HITL gate node — deliver_manual
# ---------------------------------------------------------------------------


async def test_hitl_gate_resume_with_deliver_manual():
    """deliver_manual returns manual_output in state and correct result."""
    gate_config = {"gate_id": "review-step"}
    node_fn = make_hitl_gate_fn(gate_config)

    manual_output = {"summary": "Manually provided", "approved": True}
    result = await node_fn(
        {
            "artifacts": [],
            "_hitl_decision": {"action": "deliver_manual", "output": manual_output},
        }
    )

    assert len(result["artifacts"]) == 1
    assert result["artifacts"][0]["result"] == "delivered_manual"
    assert result["artifacts"][0]["manual_output"] == manual_output
    assert result["output"] == manual_output
    assert result["artifacts"][0]["human_data"] == {
        "action": "deliver_manual",
        "output": manual_output,
    }


async def test_hitl_gate_resume_with_deliver_manual_empty_output():
    """deliver_manual with empty output returns empty dict, not a crash."""
    gate_config = {"gate_id": "review-step"}
    node_fn = make_hitl_gate_fn(gate_config)

    result = await node_fn(
        {
            "artifacts": [],
            "_hitl_decision": {"action": "deliver_manual", "output": {}},
        }
    )

    assert result["artifacts"][0]["result"] == "delivered_manual"
    assert not result["output"]
    assert not result["artifacts"][0]["manual_output"]


# ---------------------------------------------------------------------------
# HITL gate node — modify-then-approve
# ---------------------------------------------------------------------------


async def test_hitl_gate_resume_with_modified_output_writes_output_key():
    """When _hitl_decision contains modified_output, it is written to state as `output`."""
    gate_config = {"gate_id": "modify-gate"}
    node_fn = make_hitl_gate_fn(gate_config)

    modified = {"summary": "Human-edited output", "approved": True}
    result = await node_fn(
        {
            "artifacts": [],
            "_hitl_decision": {"action": "approved", "modified_output": modified},
        }
    )

    assert result["output"] == modified
    assert result["artifacts"][0]["result"] == "approved"
    assert result["artifacts"][0]["human_data"]["modified_output"] == modified


async def test_hitl_gate_resume_without_modified_output_skips_output_key():
    """Regular approval without modified_output does NOT write an `output` key."""
    gate_config = {"gate_id": "plain-approve"}
    node_fn = make_hitl_gate_fn(gate_config)

    result = await node_fn(
        {
            "artifacts": [],
            "_hitl_decision": {"action": "approved", "notes": "Looks good"},
        }
    )

    assert "output" not in result
    assert result["artifacts"][0]["result"] == "approved"


# ---------------------------------------------------------------------------
# _evaluate_eval_condition pure function
# ---------------------------------------------------------------------------


class TestEvaluateEvalCondition:
    def test_lt_below_threshold_true(self):
        assert _evaluate_eval_condition(0.4, 0.8, "lt") is True

    def test_lt_equal_threshold_false(self):
        assert _evaluate_eval_condition(0.8, 0.8, "lt") is False

    def test_lt_above_threshold_false(self):
        assert _evaluate_eval_condition(0.9, 0.8, "lt") is False

    def test_gt_above_threshold_true(self):
        assert _evaluate_eval_condition(0.9, 0.8, "gt") is True

    def test_gt_below_threshold_false(self):
        assert _evaluate_eval_condition(0.4, 0.8, "gt") is False

    def test_lte_at_threshold_true(self):
        assert _evaluate_eval_condition(0.8, 0.8, "lte") is True

    def test_lte_below_threshold_true(self):
        assert _evaluate_eval_condition(0.5, 0.8, "lte") is True

    def test_gte_at_threshold_true(self):
        assert _evaluate_eval_condition(0.8, 0.8, "gte") is True

    def test_gte_above_threshold_true(self):
        assert _evaluate_eval_condition(0.9, 0.8, "gte") is True

    def test_eq_matching_true(self):
        assert _evaluate_eval_condition(0.8, 0.8, "eq") is True

    def test_eq_not_matching_false(self):
        assert _evaluate_eval_condition(0.7, 0.8, "eq") is False

    def test_neq_different_true(self):
        assert _evaluate_eval_condition(0.7, 0.8, "neq") is True

    def test_neq_same_false(self):
        assert _evaluate_eval_condition(0.8, 0.8, "neq") is False

    def test_unknown_operator_false(self):
        assert _evaluate_eval_condition(0.8, 0.8, "unknown") is False


# ---------------------------------------------------------------------------
# Eval-before-interrupt — persistence of eval results (§8.17)
# ---------------------------------------------------------------------------


class _RecordingSession:
    """Fake async session that records ``EvalResultModel`` rows added."""

    def __init__(self) -> None:
        self.added: list[Any] = []

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    def begin(self) -> "_RecordingSession":
        return self

    def add(self, obj: Any) -> None:
        self.added.append(obj)


async def test_eval_before_interrupt_persists_results(monkeypatch: pytest.MonkeyPatch):
    """Eval results are written to the ``eval_results`` table via session_factory.

    With ``session_factory`` + ``org_id`` wired (as the executor does), the
    gate persists an ``EvalResultModel`` row per node-scoped eval definition
    before raising the interrupt — so post-run suite-level threshold checks
    (``_check_eval_suites``) can read committed results.
    """
    import uuid as _uuid

    from modulo.db.models.eval_result import EvalResult as EvalResultModel

    org_id = _uuid.UUID("00000000-0000-0000-0000-0000000000a1")
    run_id = _uuid.UUID("00000000-0000-0000-0000-0000000000b2")
    eval_id = _uuid.UUID("00000000-0000-0000-0000-0000000000c3")
    node_uuid = _uuid.UUID("00000000-0000-0000-0000-0000000000d4")
    session = _RecordingSession()

    @asynccontextmanager
    async def _fake_factory():
        yield session

    monkeypatch.setattr("modulo.core.pipeline_engine.node_runner.set_rls_org", AsyncMock())
    monkeypatch.setattr("modulo.core.pipeline_engine.node_runner.set_rls_execution_context", AsyncMock())

    gate_config = {"gate_id": "persist-gate"}
    eval_def = EvalDefinition(
        id=eval_id,
        org_id=org_id,
        node_id=str(node_uuid),
        name="quality-check",
        eval_type=EvalType.REGEX,
        config={"pattern": "pass", "field": "level"},
        failure_behaviour="warn",
    )
    node_fn = make_hitl_gate_fn(
        gate_config,
        eval_definitions=[eval_def],
        session_factory=_fake_factory,
        org_id=org_id,
    )

    with pytest.raises(GraphInterrupt):
        await node_fn(
            {
                "artifacts": [],
                "_hitl_gates": [],
                "_run_id": run_id,
                "level": "fail",
            }
        )

    assert len(session.added) == 1
    row = session.added[0]
    assert isinstance(row, EvalResultModel)
    assert row.organisation_id == org_id
    assert row.run_id == run_id
    assert row.node_id == node_uuid
    assert row.eval_id == eval_id
    assert row.passed is False
    assert row.score == 0.0


async def test_eval_before_interrupt_persist_failure_does_not_block_interrupt(monkeypatch: pytest.MonkeyPatch):
    """A persistence failure is logged, not raised — the gate still interrupts.

    Eval-result persistence is best-effort (the run must not die because the
    ``eval_results`` write failed); the interrupt still fires and the run
    proceeds to ``awaiting_human``.
    """
    import uuid as _uuid

    org_id = _uuid.UUID("00000000-0000-0000-0000-0000000000a1")
    run_id = _uuid.UUID("00000000-0000-0000-0000-0000000000b2")

    @asynccontextmanager
    async def _boom_factory():
        raise RuntimeError("db unavailable")
        yield  # pragma: no cover - make asynccontextmanager a valid generator

    monkeypatch.setattr("modulo.core.pipeline_engine.node_runner.set_rls_org", AsyncMock())
    monkeypatch.setattr("modulo.core.pipeline_engine.node_runner.set_rls_execution_context", AsyncMock())

    eval_def = EvalDefinition(
        id=_uuid.UUID("00000000-0000-0000-0000-0000000000c3"),
        org_id=org_id,
        name="quality-check",
        eval_type=EvalType.REGEX,
        config={"pattern": "pass", "field": "level"},
        failure_behaviour="warn",
    )
    node_fn = make_hitl_gate_fn(
        {"gate_id": "persist-fail-gate"},
        eval_definitions=[eval_def],
        session_factory=_boom_factory,
        org_id=org_id,
    )

    with pytest.raises(GraphInterrupt):
        await node_fn(
            {
                "artifacts": [],
                "_hitl_gates": [],
                "_run_id": run_id,
                "level": "fail",
            }
        )


async def test_eval_before_interrupt_skips_persist_without_run_id(monkeypatch: pytest.MonkeyPatch):
    """No ``_run_id`` in state → no eval-result rows are written (nothing to key)."""
    import uuid as _uuid

    org_id = _uuid.UUID("00000000-0000-0000-0000-0000000000a1")
    session = _RecordingSession()

    async def _fake_factory():
        return session

    monkeypatch.setattr("modulo.core.pipeline_engine.node_runner.set_rls_org", AsyncMock())
    monkeypatch.setattr("modulo.core.pipeline_engine.node_runner.set_rls_execution_context", AsyncMock())

    eval_def = EvalDefinition(
        id=_uuid.UUID("00000000-0000-0000-0000-0000000000c3"),
        org_id=org_id,
        name="quality-check",
        eval_type=EvalType.REGEX,
        config={"pattern": "pass", "field": "level"},
        failure_behaviour="warn",
    )
    node_fn = make_hitl_gate_fn(
        {"gate_id": "no-runid-gate"},
        eval_definitions=[eval_def],
        session_factory=_fake_factory,
        org_id=org_id,
    )

    with pytest.raises(GraphInterrupt):
        await node_fn(
            {
                "artifacts": [],
                "_hitl_gates": [],
                "level": "fail",
            }
        )

    assert not session.added


# ---------------------------------------------------------------------------
# FAR-311 regression: gate evals target the SOURCE node's contract output
# ---------------------------------------------------------------------------

PR_REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"status": {"type": "string"}},
    "if": {"properties": {"status": {"const": "completed"}}},
    "then": {"required": ["pr_url", "changed_files"]},
    "required": ["status"],
}


def _pr_review_eval_def() -> EvalDefinition:
    return EvalDefinition(
        id=uuid4(),
        org_id=uuid4(),
        node_id="reviewer",
        name="pr-review",
        eval_type=EvalType.JSON_SCHEMA,
        config={"schema": PR_REVIEW_SCHEMA},
        failure_behaviour="block",
    )


def _completed_sandbox_gate_state(contract_output: dict[str, Any]) -> dict[str, Any]:
    """A gate state after a sandbox_agent source node completed.

    LangGraph merges the node's envelope at the state's TOP-LEVEL keys:
    ``output`` holds the telemetry-style outer envelope, ``artifacts`` the
    node's artifact wrapper (contract return in ``output.output_json``).
    """
    return {
        "artifacts": [
            {
                "node_id": "reviewer",
                "status": "completed",
                "output": {"status": "completed", "summary": "reviewed", "output_json": contract_output},
            }
        ],
        "output": {
            "status": "completed",
            "summary": "reviewed",
            "wall_clock_time_ms": 5,
            "cost_estimate_usd": 0.01,
        },
        "_hitl_gates": [],
        "run_context": {},
    }


async def test_gate_eval_validates_source_contract_output_for_sandbox_agent():
    """A source sandbox_agent whose artifact ``output_json`` carries pr_url +
    changed_files PASSES the pr_url-requiring eval (the merged-state ``output``
    telemetry alone would fail it)."""
    node_fn = make_hitl_gate_fn(
        {"gate_id": "eval-gate"},
        eval_definitions=[_pr_review_eval_def()],
        node_type_map={"reviewer": "sandbox_agent"},
    )

    with pytest.raises(GraphInterrupt):
        await node_fn(
            _completed_sandbox_gate_state(
                {"status": "completed", "pr_url": "https://github.com/x/y/pull/1", "changed_files": ["a.py"]}
            )
        )


async def test_gate_eval_blocks_on_contract_output_missing_pr_url():
    """Same graph, but the source's contract output lacks pr_url/changed_files
    → the block eval fails with EvalBlockedError (not an interrupt)."""
    node_fn = make_hitl_gate_fn(
        {"gate_id": "eval-gate"},
        eval_definitions=[_pr_review_eval_def()],
        node_type_map={"reviewer": "sandbox_agent"},
    )

    with pytest.raises(EvalBlockedError, match="pr-review"):
        await node_fn(_completed_sandbox_gate_state({"status": "completed", "summary": "no pr was made"}))


async def test_gate_eval_without_type_map_keeps_whole_state_target():
    """No node_type_map → the gate evaluates the whole state as before (the
    telemetry-only merged state fails the pr_url schema, documenting the
    pre-fix behaviour)."""
    node_fn = make_hitl_gate_fn(
        {"gate_id": "eval-gate"},
        eval_definitions=[_pr_review_eval_def()],
    )

    with pytest.raises(EvalBlockedError, match="pr-review"):
        await node_fn(
            _completed_sandbox_gate_state(
                {"status": "completed", "pr_url": "https://github.com/x/y/pull/1", "changed_files": ["a.py"]}
            )
        )


async def test_gate_eval_uses_sources_own_output_for_agent_in_fanout():
    """FAR-311: an ``agent`` source's contract output is its envelope's
    ``output``. In a parallel fan-out the merged ``state["output"]`` is
    last-write-wins and can belong to a sibling — the gate must take the
    source's own output from its matched artifact, not the merged key."""
    node_fn = make_hitl_gate_fn(
        {"gate_id": "eval-gate"},
        eval_definitions=[_pr_review_eval_def()],
        node_type_map={"reviewer": "agent"},
    )
    # Sibling "writer" lands last and its top-level `output` (no pr_url) is
    # what the merged state's ``output`` key holds; the gated agent source
    # "reviewer" carries the valid pr_url in its own artifact output.
    state = {
        "artifacts": [
            {
                "node_id": "reviewer",
                "status": "completed",
                "output": {"status": "completed", "pr_url": "https://github.com/x/y/pull/1", "changed_files": ["a.py"]},
            },
            {
                "node_id": "writer",
                "status": "completed",
                "output": {"status": "completed", "summary": "sibling wrote files"},
            },
        ],
        "output": {"status": "completed", "summary": "sibling wrote files"},
        "_hitl_gates": [],
        "run_context": {},
    }

    with pytest.raises(GraphInterrupt):
        await node_fn(state)


async def test_gate_eval_ignores_sibling_artifact_in_parallel_fanout():
    """FAR-311: the gate locates the source node's artifact by node_id, NOT by
    position. Parallel fan-out concatenates artifacts in completion order, so a
    sibling's artifact can land last — it must NEVER be mistaken for the
    source's contract output."""
    node_fn = make_hitl_gate_fn(
        {"gate_id": "eval-gate"},
        eval_definitions=[_pr_review_eval_def()],
        node_type_map={"reviewer": "sandbox_agent"},
    )
    # Sibling "writer" completes last in the artifact list but carries NO
    # pr_url; the gated source "reviewer" (earlier in the list) does.
    state = {
        "artifacts": [
            {
                "node_id": "reviewer",
                "status": "completed",
                "output": {
                    "status": "completed",
                    "summary": "reviewed",
                    "output_json": {
                        "status": "completed",
                        "pr_url": "https://github.com/x/y/pull/1",
                        "changed_files": ["a.py"],
                    },
                },
            },
            {
                "node_id": "writer",
                "status": "completed",
                "output": {
                    "status": "completed",
                    "summary": "sibling wrote files",
                    "output_json": {"status": "completed", "summary": "no pr made"},
                },
            },
        ],
        "output": {"status": "completed", "summary": "reviewed", "wall_clock_time_ms": 5, "cost_estimate_usd": 0.01},
        "_hitl_gates": [],
        "run_context": {},
    }

    with pytest.raises(GraphInterrupt):
        await node_fn(state)


# ---------------------------------------------------------------------------
# S3776 decomposition helpers (FAR-310) — direct coverage for extracted helpers
# ---------------------------------------------------------------------------


class TestBuildHitlGateArtifact:
    def test_builds_standard_envelope(self) -> None:
        artifact = _build_hitl_gate_artifact("review-step", "condition_skipped")
        assert artifact == {"artifacts": [{"node_id": "review-step", "status": "condition_skipped"}]}

    def test_merges_extra_keys_into_artifact_entry(self) -> None:
        artifact = _build_hitl_gate_artifact("g1", "skipped", autonomy="fully_autonomous", condition_result=False)
        assert artifact["artifacts"] == [
            {"node_id": "g1", "status": "skipped", "autonomy": "fully_autonomous", "condition_result": False}
        ]


class TestHitlGateConditionSkip:
    def test_returns_none_when_no_condition_configured(self) -> None:
        assert _hitl_gate_condition_skip("g1", None, {}) is None

    def test_returns_none_when_condition_is_truthy(self) -> None:
        state = {"config": {"flag": True, "score": 5}}
        assert _hitl_gate_condition_skip("g1", "config.flag", state) is None
        assert _hitl_gate_condition_skip("g1", "config.score > `3`", state) is None

    def test_returns_skip_artifact_when_condition_is_falsy(self) -> None:
        state = {"config": {"flag": False, "score": 0}}
        skip = _hitl_gate_condition_skip("g1", "config.flag", state)
        assert skip == {
            "artifacts": [
                {
                    "node_id": "g1",
                    "status": "condition_skipped",
                    "condition": "config.flag",
                    "condition_result": False,
                }
            ]
        }

    def test_returns_skip_artifact_for_null_result(self) -> None:
        """A JMESPath that matches nothing resolves to None — treated as falsy."""
        skip = _hitl_gate_condition_skip("g1", "config.missing", {"config": {}})
        assert skip is not None
        assert skip["artifacts"][0]["status"] == "condition_skipped"
        assert skip["artifacts"][0]["condition_result"] is None

    def test_invalid_jmespath_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match=r"Invalid HITL gate condition expression: foo\["):
            _hitl_gate_condition_skip("g1", "foo[", {})


class TestHitlGateAutonomyResult:
    def test_fully_autonomous_returns_skipped_artifact(self) -> None:
        state = {"run_context": {"_pipeline_default_autonomy": "fully_autonomous"}}
        autonomy, result = _hitl_gate_autonomy_result("g1", state, human_only=False)
        assert autonomy.value == "fully_autonomous"
        assert result == {"artifacts": [{"node_id": "g1", "status": "skipped", "autonomy": "fully_autonomous"}]}

    def test_notify_on_complete_returns_auto_approved_artifact(self) -> None:
        state = {"run_context": {"_pipeline_default_autonomy": "notify_on_complete"}}
        autonomy, result = _hitl_gate_autonomy_result("g1", state, human_only=False)
        assert autonomy.value == "notify_on_complete"
        assert result == {"artifacts": [{"node_id": "g1", "status": "auto_approved", "autonomy": "notify_on_complete"}]}

    def test_manual_approval_returns_no_artifact(self) -> None:
        state = {"run_context": {"_pipeline_default_autonomy": "manual_approval"}}
        autonomy, result = _hitl_gate_autonomy_result("g1", state, human_only=False)
        assert autonomy.value == "manual_approval"
        assert result is None

    def test_human_only_overrides_autonomy_skip(self) -> None:
        """human_only gates always interrupt even at fully_autonomous."""
        state = {"run_context": {"_pipeline_default_autonomy": "fully_autonomous"}}
        autonomy, result = _hitl_gate_autonomy_result("g1", state, human_only=True)
        assert autonomy.value == "fully_autonomous"
        assert result is None
