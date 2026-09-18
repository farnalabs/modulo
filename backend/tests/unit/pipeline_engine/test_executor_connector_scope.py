"""Tests for FAR-435 executor run-level connector fetch scope wiring.

Covers the pure run-scope computation (``compute_run_fetch_scope`` — the
UNION of node ``capability_scope.allowed_connectors`` and referenced Agents'
``connector_type_refs`` grants) and the ``_init_connector_hub(graph_json=...)``
wiring that passes the derived scope to ``ConnectorHub.initialise``, so the
deny-by-default fetch-time gate applies at run level while agent nodes keep
runtime access to their granted connectors.
"""

import uuid
from contextlib import ExitStack
from types import SimpleNamespace
from typing import Any, Self
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.capability_scope import compute_run_fetch_scope
from modulo.core.pipeline_engine.executor import PipelineExecutor

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


# ---------------------------------------------------------------------------
# Pure run-scope computation: compute_run_fetch_scope
# ---------------------------------------------------------------------------


def test_node_scopes_and_agent_grants_are_unioned():
    """The run scope is the UNION of node capability_scope + agent grants."""
    gh = str(uuid.uuid4())
    sl = str(uuid.uuid4())
    graph = {
        "nodes": [
            # Node with an explicit node-level scope (instance-id + type).
            {"id": "n1", "capability_scope": {"allowed_connectors": ["github", gh]}},
            # Agent node with NO static connector binding: it tool-calls the hub
            # at runtime, so its grants must be unioned in for access to survive.
            {"id": "n2", "agent_id": str(uuid.uuid4())},
        ]
    }
    grants = {str(graph["nodes"][1]["agent_id"]): {"slack", sl}}
    result = compute_run_fetch_scope(graph, grants)
    assert set(result) == {"github", "slack", gh, sl}


def test_node_without_capability_scope_falls_back_to_agent_grants():
    """A node with no explicit capability_scope is narrowed to its Agent grants
    (NOT unrestricted) — the FAR-435 security win over computing only node scopes."""
    agent_id = str(uuid.uuid4())
    graph = {"nodes": [{"id": "n1", "agent_id": agent_id}]}
    grants = {agent_id: {"github", "slack"}}
    assert set(compute_run_fetch_scope(graph, grants)) == {"github", "slack"}


def test_empty_union_returns_none():
    """An empty union (no node scopes AND no agent grants) → None == unrestricted."""
    graph = {"nodes": [{"node_type": "transform"}, {"id": "n2", "agent_id": str(uuid.uuid4())}]}
    assert compute_run_fetch_scope(graph, None) is None
    assert compute_run_fetch_scope(None, None) is None
    assert compute_run_fetch_scope({"nodes": []}, {"a": {"github"}}) is None


def test_run_scope_is_superset_of_any_single_node_scope():
    """The run scope is as broad as any single node's scope, so a per-node
    narrow-gate can never be defeated by an over-tight run scope."""
    inst = str(uuid.uuid4())
    graph = {
        "nodes": [
            {"id": "n1", "capability_scope": {"allowed_connectors": ["github", inst]}},
            {"id": "n2", "capability_scope": {"allowed_connectors": ["slack", inst]}},
        ]
    }
    result = set(compute_run_fetch_scope(graph, None))
    for node in graph["nodes"]:
        node_scope = set(node["capability_scope"]["allowed_connectors"])
        assert node_scope.issubset(result)
    assert result == {"github", "slack", inst}


def test_agent_runtime_access_preserved():
    """An agent node with no capability_scope still reaches its granted
    connectors at runtime — its connector_type_refs grants are in the run scope."""
    agent_id = str(uuid.uuid4())
    graph = {"nodes": [{"id": "n1", "agent_id": agent_id, "node_type": "agent"}]}
    grants = {agent_id: {"github", "slack"}}
    result = compute_run_fetch_scope(graph, grants)
    assert set(result) == {"github", "slack"}


def test_malformed_nodes_and_non_dict_graph_tolerated():
    graph = {"nodes": [None, "junk", {"id": "x"}, {"capability_scope": {"allowed_connectors": ["github"]}}]}
    assert compute_run_fetch_scope(graph) == ["github"]
    assert compute_run_fetch_scope([], None) is None


# ---------------------------------------------------------------------------
# Wiring: _init_connector_hub(graph_json=...) forwards the derived scope
# ---------------------------------------------------------------------------


class _FakeScalar:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> "_FakeScalar":
        return self

    def all(self) -> list[Any]:
        return self._rows


class _FakeAsyncCM:
    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None


class _FakeSession:
    def __init__(self, *, connector_rows: list[Any], agent_rows: list[Any] | None = None) -> None:
        self._connector_rows = connector_rows
        self._agent_rows = agent_rows or []

    def begin(self) -> _FakeAsyncCM:
        return _FakeAsyncCM()

    async def execute(self, stmt: Any) -> _FakeScalar:
        entity = stmt.column_descriptions[0]["entity"]
        if entity.__name__ == "Agent":
            return _FakeScalar(self._agent_rows)
        return _FakeScalar(self._connector_rows)


class _FakeSessionCM:
    def __init__(self, session: _FakeSession) -> None:
        self._session = session

    async def __aenter__(self) -> _FakeSession:
        return self._session

    async def __aexit__(self, *_: object) -> None:
        return None


def _patch_hub_construction(mock_hub: MagicMock) -> ExitStack:
    stack = ExitStack()
    stack.enter_context(patch("modulo.core.pipeline_engine.executor.set_rls_org", new=AsyncMock()))
    stack.enter_context(patch("modulo.core.pipeline_engine.executor.set_rls_execution_context", new=AsyncMock()))
    stack.enter_context(patch("modulo.core.connector_hub.ConnectorHub", return_value=mock_hub))
    stack.enter_context(patch("modulo.core.pipeline_engine.decorator.set_connector_hub"))
    stack.enter_context(patch("modulo.core.runtime_provider.create_default_hub", return_value=MagicMock()))
    stack.enter_context(patch("modulo.core.secrets_backend.create_secrets_backend", return_value=MagicMock()))
    stack.enter_context(patch("modulo.settings.get_settings", return_value=MagicMock(fernet_key="test")))
    return stack


@pytest.mark.asyncio
async def test_init_connector_hub_forwards_node_scope_to_initialise():
    """A run-level fetch scope derived from a scoped graph is forwarded as
    ``allowed_connectors`` so the deny-by-default fetch-time gate applies at
    run level (FAR-435 building on FAR-418)."""
    executor = PipelineExecutor(MagicMock())
    executor._session_factory = lambda: _FakeSessionCM(_FakeSession(connector_rows=[MagicMock()]))

    mock_hub = MagicMock()
    mock_hub.initialise = AsyncMock()
    mock_hub.__aenter__ = AsyncMock(return_value=mock_hub)
    mock_hub.__aexit__ = AsyncMock()
    graph_json = {
        "nodes": [
            {"capability_scope": {"allowed_connectors": ["github"]}},
            {"capability_scope": {"allowed_connectors": ["slack"]}},
        ]
    }
    with _patch_hub_construction(mock_hub):
        hub = await executor._init_connector_hub(org_id=uuid.uuid4(), graph_json=graph_json)

    assert hub is mock_hub
    mock_hub.initialise.assert_awaited_once()
    call = mock_hub.initialise.await_args
    assert set(call.kwargs["allowed_connectors"]) == {"github", "slack"}


@pytest.mark.asyncio
async def test_init_connector_hub_forwards_agent_grants_to_initialise():
    """An agent node (no capability_scope) contributes its connector_type_refs
    grants to the run scope, so runtime agent access is preserved while the hub
    stays deny-by-default."""
    agent_id = uuid.uuid4()
    executor = PipelineExecutor(MagicMock())
    fake_session = _FakeSession(
        connector_rows=[MagicMock()],
        agent_rows=[SimpleNamespace(id=agent_id, connector_type_refs=["github", "slack"])],
    )
    executor._session_factory = lambda: _FakeSessionCM(fake_session)

    mock_hub = MagicMock()
    mock_hub.initialise = AsyncMock()
    mock_hub.__aenter__ = AsyncMock(return_value=mock_hub)
    mock_hub.__aexit__ = AsyncMock()
    graph_json = {
        "nodes": [
            {"id": "agent-node", "node_type": "agent", "agent_id": str(agent_id)},
        ]
    }
    with _patch_hub_construction(mock_hub):
        hub = await executor._init_connector_hub(org_id=uuid.uuid4(), graph_json=graph_json)

    assert hub is mock_hub
    call = mock_hub.initialise.await_args
    assert set(call.kwargs["allowed_connectors"]) == {"github", "slack"}


@pytest.mark.asyncio
async def test_init_connector_hub_none_scope_is_unrestricted():
    """A graph with no connector fetch scope nor agent grants yields
    ``allowed_connectors=None`` (the pre-scope, fetch-everything behaviour)."""
    executor = PipelineExecutor(MagicMock())
    executor._session_factory = lambda: _FakeSessionCM(_FakeSession(connector_rows=[MagicMock()]))

    mock_hub = MagicMock()
    mock_hub.initialise = AsyncMock()
    mock_hub.__aenter__ = AsyncMock(return_value=mock_hub)
    mock_hub.__aexit__ = AsyncMock()
    graph_json = {"nodes": [{"node_type": "transform"}]}
    with _patch_hub_construction(mock_hub):
        hub = await executor._init_connector_hub(org_id=uuid.uuid4(), graph_json=graph_json)

    assert hub is mock_hub
    call = mock_hub.initialise.await_args
    assert call.kwargs["allowed_connectors"] is None
