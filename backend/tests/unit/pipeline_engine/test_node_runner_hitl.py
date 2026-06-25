"""Unit tests for HITL gate and manual node functions."""

from typing import Any

import pytest
from langgraph.errors import NodeInterrupt

from modulo.core.pipeline_engine.node_runner import make_hitl_gate_fn, make_manual_node_fn

# ---------------------------------------------------------------------------
# HITL gate node — first invocation (raises NodeInterrupt)
# ---------------------------------------------------------------------------


async def test_hitl_gate_first_call_raises_interrupt():
    gate_config = {"gate_id": "review-step", "human_only": False}
    node_fn = make_hitl_gate_fn(gate_config)

    with pytest.raises(NodeInterrupt) as exc_info:
        await node_fn({"artifacts": [], "_hitl_gates": []})

    # NodeInterrupt(value) stores value in args as [Interrupt(value, ...)].
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
    with pytest.raises(NodeInterrupt):
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
    with pytest.raises(NodeInterrupt):
        await node_fn(state)

    assert len(state["_hitl_gates"]) == 2


# ---------------------------------------------------------------------------
# HITL gate node — resume (state has _hitl_decision)
# ---------------------------------------------------------------------------


async def test_hitl_gate_resume_with_approved():
    gate_config = {"gate_id": "review-step"}
    node_fn = make_hitl_gate_fn(gate_config)

    result = await node_fn({
        "artifacts": [],
        "_hitl_decision": {"action": "approved", "notes": "Looks good"},
    })

    assert len(result["artifacts"]) == 1
    assert result["artifacts"][0]["result"] == "approved"
    assert result["artifacts"][0]["human_data"] == {"action": "approved", "notes": "Looks good"}


async def test_hitl_gate_resume_with_rejected():
    gate_config = {"gate_id": "review-step"}
    node_fn = make_hitl_gate_fn(gate_config)

    result = await node_fn({
        "artifacts": [],
        "_hitl_decision": {"action": "rejected", "reason": "Not good enough"},
    })

    assert result["artifacts"][0]["result"] == "rejected"


async def test_hitl_gate_resume_preserves_existing_artifacts():
    gate_config = {"gate_id": "review-step"}
    prior_artifact = {"node_id": "prior-node", "status": "executed"}
    node_fn = make_hitl_gate_fn(gate_config)

    result = await node_fn({
        "artifacts": [prior_artifact],
        "_hitl_decision": {"action": "approved"},
    })

    assert len(result["artifacts"]) == 2
    assert result["artifacts"][0] == prior_artifact


# ---------------------------------------------------------------------------
# Manual node — first invocation (raises NodeInterrupt)
# ---------------------------------------------------------------------------


async def test_manual_node_first_call_raises_interrupt():
    node_def = {"id": "manual-node-1", "node_type": "manual"}
    node_fn = make_manual_node_fn(node_def)

    with pytest.raises(NodeInterrupt) as exc_info:
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


async def test_manual_node_resume_with_output():
    node_def = {"id": "manual-node-1", "node_type": "manual"}
    node_fn = make_manual_node_fn(node_def)

    result = await node_fn({
        "artifacts": [],
        "_hitl_decision": {"action": "manual_output", "output": {"title": "Test"}},
    })

    assert result["manual_output"] == {"title": "Test"}


async def test_manual_node_resume_validates_required_fields():
    node_def = {
        "id": "manual-node-2",
        "node_type": "manual",
        "output_schema_json": {"required": ["title", "body"]},
    }
    node_fn = make_manual_node_fn(node_def)

    with pytest.raises(ValueError, match="missing required field"):
        await node_fn({
            "artifacts": [],
            "_hitl_decision": {"action": "manual_output", "output": {"title": "Only title"}},
        })


async def test_manual_node_resume_without_schema_passes_any_data():
    node_def = {"id": "manual-node-3", "node_type": "manual"}
    node_fn = make_manual_node_fn(node_def)

    result = await node_fn({
        "artifacts": [],
        "_hitl_decision": {"action": "manual_output", "output": {"anything": 42}},
    })

    assert result["manual_output"] == {"anything": 42}


# ---------------------------------------------------------------------------
# HITL gate node — autonomy level integration
# ---------------------------------------------------------------------------


async def test_hitl_gate_fully_autonomous_skips_gate():
    gate_config = {"gate_id": "auto-gate", "human_only": False}
    node_fn = make_hitl_gate_fn(gate_config)

    result = await node_fn({
        "artifacts": [],
        "run_context": {
            "_pipeline_default_autonomy": "fully_autonomous",
        },
    })

    assert len(result["artifacts"]) == 1
    assert result["artifacts"][0]["status"] == "skipped"
    assert result["artifacts"][0]["autonomy"] == "fully_autonomous"


async def test_hitl_gate_notify_on_complete_auto_approves():
    gate_config = {"gate_id": "notify-gate", "human_only": False}
    node_fn = make_hitl_gate_fn(gate_config)

    result = await node_fn({
        "artifacts": [],
        "run_context": {
            "_pipeline_default_autonomy": "notify_on_complete",
        },
    })

    assert len(result["artifacts"]) == 1
    assert result["artifacts"][0]["status"] == "auto_approved"
    assert result["artifacts"][0]["autonomy"] == "notify_on_complete"


async def test_hitl_gate_run_context_recommendation_overrides_pipeline_default():
    gate_config = {"gate_id": "rec-gate", "human_only": False}
    node_fn = make_hitl_gate_fn(gate_config)

    result = await node_fn({
        "artifacts": [],
        "run_context": {
            "_pipeline_default_autonomy": "manual_approval",
            "autonomy_recommendation": "fully_autonomous",
        },
    })

    assert result["artifacts"][0]["status"] == "skipped"
    assert result["artifacts"][0]["autonomy"] == "fully_autonomous"


async def test_hitl_gate_human_only_overrides_fully_autonomous():
    gate_config = {"gate_id": "human-override", "human_only": True}
    node_fn = make_hitl_gate_fn(gate_config)

    with pytest.raises(NodeInterrupt) as exc_info:
        await node_fn({
            "artifacts": [],
            "_hitl_gates": [],
            "run_context": {
                "_pipeline_default_autonomy": "fully_autonomous",
            },
        })

    interrupt_list = exc_info.value.args[0]
    assert interrupt_list[0].value["gate_id"] == "human-override"
    assert interrupt_list[0].value["autonomy_level"] == "fully_autonomous"
    assert interrupt_list[0].value["human_only"] is True


async def test_hitl_gate_manual_approval_raises_interrupt():
    gate_config = {"gate_id": "manual-gate", "human_only": False}
    node_fn = make_hitl_gate_fn(gate_config)

    with pytest.raises(NodeInterrupt) as exc_info:
        await node_fn({
            "artifacts": [],
            "_hitl_gates": [],
            "run_context": {
                "_pipeline_default_autonomy": "manual_approval",
            },
        })

    interrupt_value = exc_info.value.args[0][0].value
    assert interrupt_value["gate_id"] == "manual-gate"
    assert interrupt_value["autonomy_level"] == "manual_approval"
    assert interrupt_value["human_only"] is False


async def test_hitl_gate_no_run_context_falls_back_to_manual_approval():
    gate_config = {"gate_id": "no-ctx-gate", "human_only": False}
    node_fn = make_hitl_gate_fn(gate_config)

    with pytest.raises(NodeInterrupt):
        await node_fn({
            "artifacts": [],
            "_hitl_gates": [],
        })

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
    assert len(state.get("_hitl_gates", [])) == 0
    assert result["artifacts"][0]["status"] == "skipped"


async def test_hitl_gate_notify_on_complete_preserves_artifacts():
    prior_artifact = {"node_id": "prior", "status": "executed"}
    gate_config = {"gate_id": "notify-preserve", "human_only": False}
    node_fn = make_hitl_gate_fn(gate_config)

    result = await node_fn({
        "artifacts": [prior_artifact],
        "run_context": {"_pipeline_default_autonomy": "notify_on_complete"},
    })

    assert len(result["artifacts"]) == 2
    assert result["artifacts"][0] == prior_artifact
    assert result["artifacts"][1]["status"] == "auto_approved"
