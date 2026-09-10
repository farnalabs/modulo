"""Unit tests for the FAR-764 community execution gate (default-deny).

The gate restricts community-sourced agents that have not yet been granted by
the operator:

  * ``make_node_fn`` (LLM node) and ``make_sandbox_agent_fn`` (sandbox node)
    return a ``blocked`` / ``community_sourced_not_granted`` stub instead of
    invoking the model, proving the default-deny security property before any
    model invocation.
  * The gate compares the *canonicalised* UUID form, so a non-canonical
    rendering (uppercase / brace / no-dash) of the same agent id cannot slip
    past the gate and fail it open.
  * ``PipelineExecutor._resolve_run_connector_scope`` zeroes the connector
    grants of gated agents and records them in ``_community_gated_agents`` so
    the gate is seeded into run state.

These cover the prove-the-fix gaps from the PR review of #344.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import modulo.core.pipeline_engine.node_runner as nr
from modulo.core.pipeline_engine.executor import PipelineExecutor

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _node_state(gated: set[str] | None = None) -> dict[str, Any]:
    return {
        "run_context": {"input": {}},
        "_community_gated_agents": gated or set(),
    }


# ---------------------------------------------------------------------------
# make_node_fn (LLM node) gate
# ---------------------------------------------------------------------------


async def test_make_node_fn_blocks_gated_agent():
    """A gated agent's LLM node is blocked before model invocation."""
    agent_id = uuid.uuid4()
    fn = nr.make_node_fn({"id": "n1", "agent_id": str(agent_id)})
    result = await fn(_node_state({str(agent_id)}))
    artifact = result["artifacts"][0]
    assert artifact["status"] == "blocked"
    assert artifact["reason"] == "community_sourced_not_granted"


async def test_make_node_fn_blocks_noncanonical_agent_id():
    """A non-canonical (uppercase) agent id still matches the canonical gated
    set — the gate must NOT fail open on UUID formatting."""
    agent_id = uuid.uuid4()
    fn = nr.make_node_fn({"id": "n1", "agent_id": str(agent_id).upper()})
    result = await fn(_node_state({str(agent_id)}))
    assert result["artifacts"][0]["status"] == "blocked"


async def test_make_node_fn_allows_ungated_agent():
    """An un-gated agent executes normally (stub, no model invocation)."""
    agent_id = uuid.uuid4()
    fn = nr.make_node_fn({"id": "n1", "agent_id": str(agent_id)})
    result = await fn(_node_state(set()))
    assert result["artifacts"][0]["status"] == "executed"


# ---------------------------------------------------------------------------
# make_sandbox_agent_fn (sandbox node) gate
# ---------------------------------------------------------------------------


def _sandbox_node_def(agent_id: uuid.UUID) -> dict[str, Any]:
    return {
        "id": "n1",
        "agent_id": str(agent_id),
        "agent_prompt": "Do the thing",
        "agent_command": "echo hi",
    }


async def test_make_sandbox_agent_fn_blocks_gated_agent():
    """A gated agent's sandbox node is blocked and the real impl never runs."""
    agent_id = uuid.uuid4()
    fn = nr.make_sandbox_agent_fn(_sandbox_node_def(agent_id))
    impl = AsyncMock(return_value={"output": {"status": "completed"}})
    with patch.object(nr, "_sandbox_agent_impl", impl):
        result = await fn(_node_state({str(agent_id)}))
    artifact = result["artifacts"][0]
    assert artifact["status"] == "blocked"
    assert artifact["reason"] == "community_sourced_not_granted"
    impl.assert_not_awaited()


async def test_make_sandbox_agent_fn_blocks_noncanonical_agent_id():
    """Non-canonical agent id still blocks the sandbox node (fail-closed)."""
    agent_id = uuid.uuid4()
    node_def = _sandbox_node_def(agent_id)
    node_def["agent_id"] = str(agent_id).upper()
    fn = nr.make_sandbox_agent_fn(node_def)
    impl = AsyncMock(return_value={"output": {"status": "completed"}})
    with patch.object(nr, "_sandbox_agent_impl", impl):
        result = await fn(_node_state({str(agent_id)}))
    assert result["artifacts"][0]["status"] == "blocked"
    impl.assert_not_awaited()


async def test_make_sandbox_agent_fn_allows_ungated_agent():
    """An un-gated agent proceeds to the sandbox impl (gate not over-blocking)."""
    agent_id = uuid.uuid4()
    fn = nr.make_sandbox_agent_fn(_sandbox_node_def(agent_id))
    impl = AsyncMock(return_value={"output": {"status": "completed"}})
    with patch.object(nr, "_sandbox_agent_impl", impl):
        result = await fn(_node_state(set()))
    impl.assert_awaited_once()
    assert result["output"]["status"] == "completed"


# ---------------------------------------------------------------------------
# Executor: connector scope zeroing + gated-set seeding
# ---------------------------------------------------------------------------


class _FakeScalar:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> _FakeScalar:
        return self

    def all(self) -> list[Any]:
        return self._rows


class _FakeSession:
    def __init__(self, agent_rows: list[Any], install_rows: list[Any]) -> None:
        self._agent_rows = agent_rows
        self._install_rows = install_rows

    async def execute(self, stmt: Any) -> _FakeScalar:
        entity = stmt.column_descriptions[0]["entity"].__name__
        if entity == "Agent":
            return _FakeScalar(self._agent_rows)
        return _FakeScalar(self._install_rows)


def _make_agent(agent_id: uuid.UUID, ci_id: uuid.UUID, grants: list[str]) -> Any:
    return SimpleNamespace(
        id=agent_id,
        organisation_id=uuid.uuid4(),
        connector_type_refs=[{"connector_type": g} for g in grants],
        collection_install_id=ci_id,
    )


def _make_install(ci_id: uuid.UUID, *, community_sourced: bool, agents_granted: bool) -> Any:
    return SimpleNamespace(
        install_id=ci_id,
        community_sourced=community_sourced,
        agents_granted=agents_granted,
    )


async def test_resolve_scope_gates_community_not_granted_install():
    """A community-sourced, not-yet-granted install gates its agent and zeroes
    the agent's connector grants in the run scope."""
    org_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    ci_id = uuid.uuid4()
    agent = _make_agent(agent_id, ci_id, ["github"])
    install = _make_install(ci_id, community_sourced=True, agents_granted=False)
    executor = PipelineExecutor(MagicMock())
    graph_json = {"nodes": [{"id": "n1", "agent_id": str(agent_id)}]}

    scope = await executor._resolve_run_connector_scope(_FakeSession([agent], [install]), org_id, graph_json)

    assert executor._community_gated_agents == {str(agent_id)}
    assert scope is None or "github" not in scope


async def test_resolve_scope_keeps_granted_community_install_connectors():
    """A community install the operator HAS granted is not gated, and its
    connector grants survive in the run scope."""
    org_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    ci_id = uuid.uuid4()
    agent = _make_agent(agent_id, ci_id, ["github"])
    install = _make_install(ci_id, community_sourced=True, agents_granted=True)
    executor = PipelineExecutor(MagicMock())
    graph_json = {"nodes": [{"id": "n1", "agent_id": str(agent_id)}]}

    scope = await executor._resolve_run_connector_scope(_FakeSession([agent], [install]), org_id, graph_json)

    assert executor._community_gated_agents == set()
    assert "github" in scope


async def test_resolve_scope_keeps_non_community_install_connectors():
    """A non-community install is never gated regardless of grant state."""
    org_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    ci_id = uuid.uuid4()
    agent = _make_agent(agent_id, ci_id, ["github"])
    install = _make_install(ci_id, community_sourced=False, agents_granted=False)
    executor = PipelineExecutor(MagicMock())
    graph_json = {"nodes": [{"id": "n1", "agent_id": str(agent_id)}]}

    scope = await executor._resolve_run_connector_scope(_FakeSession([agent], [install]), org_id, graph_json)

    assert executor._community_gated_agents == set()
    assert "github" in scope
