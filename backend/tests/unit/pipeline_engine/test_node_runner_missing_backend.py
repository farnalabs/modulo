"""Unit tests for FAR-1115: agent nodes that resolve to no model backend.

The defect: an agent node (model-backed by intent) whose agent has
``model_backend_id: null`` silently returns the stub artifact (status
``executed``), reporting success with zero tokens and no model call.  The
catch-all stub branch also swallows connector-binding and manual nodes that
legitimately route here.

The fix: when ``agent_id`` is present on the node_def AND
``model_backend_id`` is falsy, raise ``NodeMissingModelBackendError`` — a
clear, actionable error naming both the node and the agent.  Nodes without
``agent_id`` (connector-binding / manual) keep the original stub behaviour.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

import modulo.core.pipeline_engine.node_runner as nr
from modulo.core.pipeline_engine.errors import NodeMissingModelBackendError
from modulo.core.pipeline_engine.node_runner import _COMPLETED_EMISSION_STATUSES

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _node_state() -> dict[str, Any]:
    return {
        "run_context": {"input": {}},
    }


# ---------------------------------------------------------------------------
# Legitimate stub path: nodes WITHOUT agent_id keep current behaviour
# ---------------------------------------------------------------------------


async def test_connector_node_without_agent_id_returns_executed_stub():
    """A connector node (no agent_id, no model_backend_id) returns the stub.

    This is the legitimate stub path — the node is not model-backed by intent.
    Before the fix, this returned {"status": "executed"} and still does.
    """
    fn = nr.make_node_fn({"id": "n-connector"})
    result = await fn(_node_state())
    artifact = result["artifacts"][0]
    assert artifact["node_id"] == "n-connector"
    assert artifact["status"] == "executed"


async def test_agent_node_without_agent_id_returns_executed_stub():
    """An agent-type node with no agent_id returns the stub.

    This covers graph_cache.py's catch-all: connector nodes without a binding
    and agent nodes without a frozen agent_id both route to make_node_fn.
    Without agent_id the node is not model-backed by intent.
    """
    fn = nr.make_node_fn({"id": "n-no-agent"})
    result = await fn(_node_state())
    artifact = result["artifacts"][0]
    assert artifact["node_id"] == "n-no-agent"
    assert artifact["status"] == "executed"


def test_executed_still_in_completed_emission_statuses():
    """The ``executed`` status must remain in _COMPLETED_EMISSION_STATUSES.

    Connector-binding and manual nodes that legitimately use the stub
    contribute completed-AND-committed artifacts to the ancestor injection
    supply.  Removing ``executed`` would break ancestor ref folding for those
    nodes.
    """
    assert "executed" in _COMPLETED_EMISSION_STATUSES
    assert "completed" in _COMPLETED_EMISSION_STATUSES


# ---------------------------------------------------------------------------
# Misconfiguration: agent node WITH agent_id but NO model_backend_id
# ---------------------------------------------------------------------------


async def test_agent_node_with_agent_id_no_backend_raises():
    """An agent node with agent_id but no model_backend_id raises.

    Before the fix, this returned {"status": "executed"} (silent success
    with zero tokens).  After the fix, it raises
    NodeMissingModelBackendError — a clear, actionable error.
    """
    agent_id = uuid.uuid4()
    fn = nr.make_node_fn({"id": "n-agent", "agent_id": str(agent_id)})
    with pytest.raises(NodeMissingModelBackendError):
        await fn(_node_state())


async def test_agent_node_no_backend_error_names_node_and_agent():
    """The error message names both the node and the agent for operator clarity."""
    agent_id = uuid.uuid4()
    fn = nr.make_node_fn({"id": "n-my-agent", "agent_id": str(agent_id)})
    with pytest.raises(NodeMissingModelBackendError, match=r"n-my-agent"):
        await fn(_node_state())
    with pytest.raises(NodeMissingModelBackendError, match=rf"{agent_id}"):
        await fn(_node_state())


async def test_agent_node_no_backend_is_not_the_stub():
    """Regression: the misconfiguration must NOT be mistaken for the stub.

    Before the fix, both paths returned the same artifact.  After the fix,
    the stub path (no agent_id) returns {"status": "executed"} while the
    misconfiguration path (agent_id present, no backend) raises.
    """
    # Legitimate stub: no agent_id
    fn_stub = nr.make_node_fn({"id": "n-stub"})
    result_stub = await fn_stub(_node_state())
    assert result_stub["artifacts"][0]["status"] == "executed"

    # Misconfiguration: agent_id present, no model_backend_id
    fn_bad = nr.make_node_fn({"id": "n-bad", "agent_id": str(uuid.uuid4())})
    with pytest.raises(NodeMissingModelBackendError):
        await fn_bad(_node_state())


# ---------------------------------------------------------------------------
# Agent node WITH both agent_id and model_backend_id proceeds normally
# ---------------------------------------------------------------------------


async def test_agent_node_with_both_id_and_backend_proceeds():
    """An agent node with both agent_id and model_backend_id invokes the model."""
    agent_id = uuid.uuid4()
    backend_id = uuid.uuid4()
    fn = nr.make_node_fn(
        {
            "id": "n-good",
            "agent_id": str(agent_id),
            "model_backend_id": str(backend_id),
        }
    )
    with patch.object(nr, "_invoke_node_model", new=AsyncMock(return_value={"result": "ok"})):
        result = await fn(_node_state())
    assert result["artifacts"][0]["status"] == "completed"
