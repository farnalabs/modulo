"""Unit tests for the manual/placeholder node type.

Tests that manual nodes:
- Are correctly dispatched by build_graph_from_json when node_type='manual'
- Do NOT require agent_id, connector_binding, or model_backend_id
- Log entries on completion (via make_manual_node_fn)
- Validate output against output_schema_id
- Interrupt for human input on first invocation
"""

import uuid
from typing import Any

import pytest
from langgraph.errors import GraphInterrupt

from modulo.core.pipeline_engine.graph_cache import _CACHE, build_graph_from_json
from modulo.core.pipeline_engine.node_runner import make_manual_node_fn


@pytest.fixture(autouse=True)
def _interrupt_without_graph_runtime_autouse(_interrupt_without_graph_runtime: None) -> None:
    """Apply the shared Interrupt()-shim fixture to every test in this module."""

    assert _interrupt_without_graph_runtime is None


def _clear_cache() -> None:
    _CACHE.clear()


# ---------------------------------------------------------------------------
# Manual node graph compilation (via build_graph_from_json)
# ---------------------------------------------------------------------------

_MANUAL_NODE_GRAPH: dict[str, Any] = {
    "nodes": [
        {
            "id": "manual-node-1",
            "node_type": "manual",
            "manual_prompt": "Enter the code review result",
        },
        {
            "id": "next-node",
            "node_type": "agent",
        },
    ],
    "edges": [
        {"source": "manual-node-1", "target": "next-node", "type": "normal"},
    ],
}


def test_manual_node_compiles_successfully():
    """A graph with a manual node should compile without error."""
    _clear_cache()
    compiled = build_graph_from_json(_MANUAL_NODE_GRAPH)
    assert compiled is not None


def test_manual_node_works_without_agent_id():
    """Manual nodes should not require agent_id in the node definition."""
    graph: dict[str, Any] = {
        "nodes": [
            {
                "id": "manual-1",
                "node_type": "manual",
                "manual_prompt": "Review the output",
                # No agent_id, no connector_binding, no model_backend_id
            },
            {
                "id": "agent-1",
                "node_type": "agent",
            },
        ],
        "edges": [
            {"source": "manual-1", "target": "agent-1", "type": "normal"},
        ],
    }
    compiled = build_graph_from_json(graph)
    assert compiled is not None


def test_manual_node_works_without_connector_binding():
    """Manual nodes should compile even without any connector_binding field."""
    graph: dict[str, Any] = {
        "nodes": [
            {
                "id": "manual-1",
                "node_type": "manual",
                # No connector_binding, no agent_id
            },
            {"id": "agent-1", "node_type": "agent"},
        ],
        "edges": [
            {"source": "manual-1", "target": "agent-1", "type": "normal"},
        ],
    }
    compiled = build_graph_from_json(graph)
    assert compiled is not None


def test_manual_node_works_without_model_backend_id():
    """Manual nodes should compile without model_backend_id."""
    graph: dict[str, Any] = {
        "nodes": [
            {
                "id": "manual-1",
                "node_type": "manual",
                "output_schema_id": str(uuid.uuid4()),
                # No model_backend_id
            },
            {"id": "agent-1", "node_type": "agent"},
        ],
        "edges": [
            {"source": "manual-1", "target": "agent-1", "type": "normal"},
        ],
    }
    compiled = build_graph_from_json(graph)
    assert compiled is not None


def test_mixed_graph_with_manual_and_agent_nodes():
    """A graph mixing manual and agent nodes should compile and run."""
    graph: dict[str, Any] = {
        "nodes": [
            {
                "id": "manual-start",
                "node_type": "manual",
                "manual_prompt": "Initialise settings",
            },
            {
                "id": "agent-process",
                "node_type": "agent",
            },
        ],
        "edges": [
            {"source": "manual-start", "target": "agent-process", "type": "normal"},
        ],
    }
    compiled = build_graph_from_json(graph)
    assert compiled is not None


def test_default_node_type_is_agent():
    """Nodes without explicit node_type should default to agent (not manual)."""
    graph: dict[str, Any] = {
        "nodes": [
            {"id": "node-a", "role": None, "agent_id": str(uuid.uuid4())},
            {"id": "node-b"},
        ],
        "edges": [
            {"source": "node-a", "target": "node-b", "type": "normal"},
        ],
    }
    compiled = build_graph_from_json(graph)
    assert compiled is not None


# ---------------------------------------------------------------------------
# Manual node function behaviour (tested directly, like node_runner tests)
# ---------------------------------------------------------------------------


async def test_manual_node_first_call_raises_interrupt():
    """A manual node should raise GraphInterrupt on first invocation."""
    node_def = {"id": "manual-unit-1", "node_type": "manual"}
    node_fn = make_manual_node_fn(node_def)

    with pytest.raises(GraphInterrupt) as exc_info:
        await node_fn({"artifacts": []})

    interrupt_list = exc_info.value.args[0]
    assert len(interrupt_list) > 0
    actual = interrupt_list[0]
    value = actual.value if hasattr(actual, "value") else actual
    assert isinstance(value, dict)
    assert value["manual"] is True
    assert value["node_id"] == "manual-unit-1"
    # FAR-541 (iteration 3): the interrupt payload stamps its own identity —
    # ``_handle_graph_interrupt`` keys the pending claim row on ``gate_id``
    # verbatim, so the manual node's row is keyed by the NODE id (previously ""
    # — invisible to the recover-node guard and the HITL decision API).
    assert value["gate_id"] == "manual-unit-1"


async def test_manual_node_awaiting_human_sets_artifact():
    """First invocation should set awaiting_human status in artifacts."""
    node_def = {"id": "manual-unit-2", "node_type": "manual"}
    node_fn = make_manual_node_fn(node_def)

    state: dict[str, Any] = {"artifacts": []}
    with pytest.raises(GraphInterrupt):
        await node_fn(state)

    # State mutation should record awaiting_human before interrupt
    assert len(state["artifacts"]) == 1
    assert state["artifacts"][0]["node_id"] == "manual-unit-2"
    assert state["artifacts"][0]["status"] == "awaiting_human"


async def test_manual_node_accepts_human_output_on_resume():
    """A manual node should process human output on resume via _hitl_decision."""
    node_def = {"id": "manual-unit-3", "node_type": "manual"}
    node_fn = make_manual_node_fn(node_def)

    result = await node_fn(
        {
            "artifacts": [],
            "_hitl_decision": {"gate_id": "manual-unit-3", "output": {"review": "approved", "comments": "LGTM"}},
        }
    )

    assert "manual_output" in result
    assert result["manual_output"] == {"review": "approved", "comments": "LGTM"}
    artifacts = result.get("artifacts", [])
    assert len(artifacts) == 1
    assert artifacts[0]["status"] == "completed"
    assert artifacts[0]["human_output"] == {"review": "approved", "comments": "LGTM"}


async def test_manual_node_validates_required_fields():
    """Manual node should raise ValueError when required fields are missing."""
    node_def = {
        "id": "manual-unit-4",
        "node_type": "manual",
        "output_schema_json": {
            "required": ["decision", "reason"],
        },
    }
    node_fn = make_manual_node_fn(node_def)

    with pytest.raises(ValueError, match="missing required field"):
        await node_fn(
            {
                "artifacts": [],
                "_hitl_decision": {"gate_id": "manual-unit-4", "output": {"decision": "approve"}},  # missing "reason"
            }
        )


async def test_manual_node_valid_output_passes_validation():
    """Valid output matching the schema should pass through."""
    node_def = {
        "id": "manual-unit-5",
        "node_type": "manual",
        "output_schema_json": {
            "required": ["decision"],
        },
    }
    node_fn = make_manual_node_fn(node_def)

    result = await node_fn(
        {
            "artifacts": [],
            "_hitl_decision": {"gate_id": "manual-unit-5", "output": {"decision": "approve", "notes": "ok"}},
        }
    )

    assert result["manual_output"]["decision"] == "approve"


async def test_manual_node_no_schema_passes_any_output():
    """Without output_schema_json, any output should pass."""
    node_def = {"id": "manual-unit-6", "node_type": "manual"}
    node_fn = make_manual_node_fn(node_def)

    result = await node_fn(
        {
            "artifacts": [],
            "_hitl_decision": {"gate_id": "manual-unit-6", "output": {"anything": 42, "nested": {"key": True}}},
        }
    )

    assert result["manual_output"] == {"anything": 42, "nested": {"key": True}}


async def test_manual_node_preserves_prior_artifacts():
    """Manual node returns delta artifacts on resume; accumulator handles merge."""
    node_def = {"id": "manual-unit-7", "node_type": "manual"}
    node_fn = make_manual_node_fn(node_def)
    prior = {"node_id": "prior-node", "status": "executed"}

    result = await node_fn(
        {
            "artifacts": [prior],
            "_hitl_decision": {"gate_id": "manual-unit-7", "output": {"data": "ok"}},
        }
    )

    assert len(result["artifacts"]) == 1
    assert result["artifacts"][0]["node_id"] == "manual-unit-7"
    assert result["artifacts"][0]["human_output"] == {"data": "ok"}


async def test_manual_node_decision_without_output_is_ignored():
    """A decision with no 'output' key must not be treated as the manual output.

    Regression: `decision.get("output", decision)` returned the whole decision
    dict (gate metadata, etc.) as the human's output when 'output' was missing.
    """
    node_def = {"id": "manual-unit-11", "node_type": "manual"}
    node_fn = make_manual_node_fn(node_def)

    result = await node_fn(
        {
            "artifacts": [],
            "_hitl_decision": {"gate_id": "manual-unit-11", "approved": True, "reviewer": "alice"},
        }
    )

    assert result.get("manual_output") is None
    assert result["artifacts"][0]["human_output"] is None


async def test_manual_node_handles_non_dict_decision():
    """If _hitl_decision output is not a dict, manual_output should be None."""
    node_def = {"id": "manual-unit-8", "node_type": "manual"}
    node_fn = make_manual_node_fn(node_def)

    result = await node_fn(
        {
            "artifacts": [],
            "_hitl_decision": {"gate_id": "manual-unit-8", "output": "plain string"},
        }
    )

    assert result.get("manual_output") is None
    assert result["artifacts"][0]["human_output"] is None


async def test_manual_node_with_output_schema_id():
    """Manual node should pass output_schema_id in the interrupt payload."""
    node_def = {
        "id": "manual-unit-9",
        "node_type": "manual",
        "output_schema_id": str(uuid.uuid4()),
    }
    node_fn = make_manual_node_fn(node_def)

    with pytest.raises(GraphInterrupt) as exc_info:
        await node_fn({"artifacts": []})

    interrupt_list = exc_info.value.args[0]
    actual = interrupt_list[0]
    value = actual.value if hasattr(actual, "value") else actual
    assert "output_schema_id" in value
    assert value["output_schema_id"] is not None


async def test_manual_node_foreign_decision_stays_interrupted():
    """FAR-541 (C1 consumer matrix): a decision stamped for a DIFFERENT node
    must never complete this manual node (historically with
    ``manual_output=None``) — the node re-interrupts and keeps waiting."""
    node_def = {"id": "manual-unit-12", "node_type": "manual"}
    node_fn = make_manual_node_fn(node_def)

    with pytest.raises(GraphInterrupt):
        await node_fn(
            {
                "artifacts": [],
                "_hitl_decision": {
                    "action": "manual_output",
                    "gate_id": "some-other-node",
                    "output": {"answer": 42},
                },
            }
        )


async def test_manual_node_unstamped_decision_stays_interrupted():
    """FAR-541: a decision without a stamp cannot be attributed to this node —
    the node re-interrupts rather than completing with foreign output."""
    node_def = {"id": "manual-unit-13", "node_type": "manual"}
    node_fn = make_manual_node_fn(node_def)

    with pytest.raises(GraphInterrupt):
        await node_fn(
            {
                "artifacts": [],
                "_hitl_decision": {"action": "manual_output", "output": {"answer": 42}},
            }
        )


# ---------------------------------------------------------------------------
# Recover-node roundtrip (FAR-541 iteration 3, FIX 3)
# ---------------------------------------------------------------------------


async def test_manual_node_completes_from_recover_node_replay_payload():
    """FAR-541 FIX 3 roundtrip (replay): recover-node stamps the recovery
    payload with the run's pending claim row's gate_id — for a manual node that
    row is keyed by the NODE id (FIX 2 stamp) — and the consumer accepts
    exactly that payload, completing with the recovery output."""
    node_def = {"id": "manual-unit-14", "node_type": "manual"}
    node_fn = make_manual_node_fn(node_def)

    # The exact payload the recover-node route dispatches when the run's
    # undecided claim row is keyed by this node's id (routes/runs.py):
    # {"action": "replay", "gate_id": <row.gate_id>, "output": <input_data>}.
    result = await node_fn(
        {
            "artifacts": [],
            "_hitl_decision": {"action": "replay", "gate_id": "manual-unit-14", "output": {"answer": 42}},
        }
    )

    assert result["manual_output"] == {"answer": 42}
    assert result["artifacts"][0]["status"] == "completed"
    assert result["artifacts"][0]["human_output"] == {"answer": 42}


async def test_manual_node_completes_from_recover_node_skip_payload():
    """FAR-541 FIX 3 roundtrip (skip): a recover-node skip (no input_data)
    stamps the same identity — the node completes with no output."""
    node_def = {"id": "manual-unit-15", "node_type": "manual"}
    node_fn = make_manual_node_fn(node_def)

    result = await node_fn(
        {
            "artifacts": [],
            "_hitl_decision": {"action": "skip", "gate_id": "manual-unit-15", "output": None},
        }
    )

    assert result.get("manual_output") is None
    assert result["artifacts"][0]["status"] == "completed"
    assert result["artifacts"][0]["human_output"] is None
