"""FAR-488a: node-level agent_commands PATCHes must reach the bound Agent row.

At snapshot time ``_apply_agent_fields`` overwrites a bound node's
``agent_commands`` with the Agent row's non-NULL value. The graph write used to
persist node-level commands to ``graph_nodes_json`` only, so an operator's
PATCH read back correctly but every run silently executed the stale Agent-row
command. These tests pin the sync ("what you PATCH is what runs") at three
levels: the pure extractor, the DB sync helper against a mocked session, and
the PUT /graph endpoint wiring.

FAR-1845: every writer of node-level ``agent_commands`` must run this sync
inside the same transaction, before the graph write. The three writers are the
REST graph-write endpoints (covered here), the MCP full-graph writer
(``test_mcp_graph_writer_syncs_agent_rows_before_the_graph_write``) and the
snapshot-rollback writer (``test_rollback_endpoint_*``). The rollback path was
the remaining divergence: it rewrote ``graph_nodes_json`` from the target
snapshot with no Agent-row sync, so a restored command read back from the graph
but was overwritten by the stale Agent row at run-snapshot time.
"""

import logging
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from modulo.api import mcp_server as ms
from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context, get_settings
from modulo.api.main import app
from modulo.api.routes.pipelines import (
    _extract_agent_command_sync_updates,
    _finalize_locked_graph_save,
    _sync_agent_row_commands,
)
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.core.pipeline_engine.git_content import GitContentRefError
from modulo.db.crud.pipeline import GraphWriteRecording
from modulo.db.crud.pipeline_snapshot import _apply_agent_fields
from modulo.db.models.agent import Agent
from modulo.settings import Settings
from tests.unit.api.mock_session import configure_mock_session

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_PIPELINE_ID = uuid.uuid4()
_SNAP_ID = uuid.UUID("44444444-4444-4444-4444-444444444444")

_REPO = "https://github.com/example/repo.git"
_SHA_A = "a" * 40
_UNPINNED_REF = f"git+{_REPO}@main#prompts/x.md"
_PINNED_REF = f"git+{_REPO}@{_SHA_A}#prompts/x.md"


def _node(node_id: uuid.UUID, *, agent_id: uuid.UUID | None, agent_commands: list[str] | None = None) -> dict[str, Any]:
    node: dict[str, Any] = {
        "id": str(node_id),
        "node_type": "sandbox_agent",
        "position": {"x": 0, "y": 0},
        "connector_binding": None,
        "template_id": "opencode",
        "agent_prompt": "do the thing",
    }
    if agent_id is not None:
        node["agent_id"] = str(agent_id)
    if agent_commands is not None:
        node["agent_commands"] = agent_commands
    return node


def _agent_node(
    node_id: uuid.UUID,
    *,
    agent_id: uuid.UUID,
    agent_commands: list[str] | None = None,
) -> dict[str, Any]:
    """An ``agent``-type bound node — the node type the graph validator's
    git-content gate does NOT check (it runs only for ``sandbox_agent``
    nodes), which is exactly the ungated shape FAR-220 closes."""
    node = _node(node_id, agent_id=agent_id, agent_commands=agent_commands)
    node["node_type"] = "agent"
    return node


def test_extract_updates_first_command_per_distinct_agent() -> None:
    """Two nodes bound to the same agent yield ONE update (first node wins);
    distinct agents each yield their own. The FULL list is carried, not just
    the first item."""
    agent_a = uuid.uuid4()
    agent_b = uuid.uuid4()
    nodes = [
        _node(uuid.uuid4(), agent_id=agent_a, agent_commands=["first"]),
        _node(uuid.uuid4(), agent_id=agent_a, agent_commands=["second"]),
        _node(uuid.uuid4(), agent_id=agent_b, agent_commands=["other"]),
    ]
    updates = _extract_agent_command_sync_updates(nodes)
    assert updates == {agent_a: ["first"], agent_b: ["other"]}


def test_extract_updates_carries_full_multi_item_list() -> None:
    """A multi-item node command list is carried in full (not truncated to the
    first item) so every command reaches the bound Agent row."""
    agent_id = uuid.uuid4()
    nodes = [_node(uuid.uuid4(), agent_id=agent_id, agent_commands=["a", "b", "c"])]
    updates = _extract_agent_command_sync_updates(nodes)
    assert updates == {agent_id: ["a", "b", "c"]}


def test_extract_updates_does_not_skip_leading_empty_item() -> None:
    """A node whose first item is empty (["", "b"]) is NOT skipped — the usable
    items still sync, so a leading empty string no longer silently drops the
    rest of the list."""
    agent_id = uuid.uuid4()
    nodes = [_node(uuid.uuid4(), agent_id=agent_id, agent_commands=["", "b"])]
    updates = _extract_agent_command_sync_updates(nodes)
    assert updates == {agent_id: ["", "b"]}


def test_extract_updates_skips_unbound_and_commandless_nodes() -> None:
    """Nodes without an agent_id, without an agent_commands, with an empty
    command, or with an unparseable agent_id are all skipped."""
    nodes = [
        _node(uuid.uuid4(), agent_id=None, agent_commands=["orphan"]),
        _node(uuid.uuid4(), agent_id=uuid.uuid4()),
        _node(uuid.uuid4(), agent_id=uuid.uuid4(), agent_commands=[""]),
        _node(uuid.uuid4(), agent_id=None),
        {"id": str(uuid.uuid4()), "position": {"x": 0, "y": 0}, "agent_id": "not-a-uuid", "agent_commands": ["x"]},
    ]
    assert not _extract_agent_command_sync_updates(nodes)


def _session_returning(agents: list[Any]) -> AsyncMock:
    session = configure_mock_session(AsyncMock())
    result = MagicMock()
    result.scalars.return_value = list(agents)
    session.execute = AsyncMock(return_value=result)
    return session


async def test_sync_updates_agent_row_when_command_differs() -> None:
    """(a) A PATCHed node command that differs from the bound Agent row's
    non-NULL command updates the Agent row."""
    agent_id = uuid.uuid4()
    agent = SimpleNamespace(id=agent_id, agent_commands=["old-command"])
    session = _session_returning([agent])
    nodes = [_node(uuid.uuid4(), agent_id=agent_id, agent_commands=["new-command"])]

    changed = await _sync_agent_row_commands(session, org_id=_ORG_ID, nodes=nodes, pipeline_id=_PIPELINE_ID)

    assert changed == [agent_id]
    assert agent.agent_commands == ["new-command"]


async def test_sync_skips_agent_row_with_null_command() -> None:
    """(d) An Agent row with a NULL agent_commands is NOT updated — the node
    value already stands at snapshot time."""
    agent_id = uuid.uuid4()
    agent = SimpleNamespace(id=agent_id, agent_commands=None)
    session = _session_returning([agent])
    nodes = [_node(uuid.uuid4(), agent_id=agent_id, agent_commands=["node-command"])]

    changed = await _sync_agent_row_commands(session, org_id=_ORG_ID, nodes=nodes, pipeline_id=_PIPELINE_ID)

    assert not changed
    assert agent.agent_commands is None


async def test_sync_noop_when_command_already_equal() -> None:
    agent_id = uuid.uuid4()
    agent = SimpleNamespace(id=agent_id, agent_commands=["same"])
    session = _session_returning([agent])
    nodes = [_node(uuid.uuid4(), agent_id=agent_id, agent_commands=["same"])]

    changed = await _sync_agent_row_commands(session, org_id=_ORG_ID, nodes=nodes, pipeline_id=_PIPELINE_ID)

    assert not changed


async def test_sync_preserves_multi_item_list() -> None:
    """A PATCHed multi-item node command list is synced to the Agent row in
    full — no truncation to a single item (FAR-488-class silent divergence)."""
    agent_id = uuid.uuid4()
    agent = SimpleNamespace(id=agent_id, agent_commands=["old-command"])
    session = _session_returning([agent])
    nodes = [_node(uuid.uuid4(), agent_id=agent_id, agent_commands=["a", "b", "c"])]

    changed = await _sync_agent_row_commands(session, org_id=_ORG_ID, nodes=nodes, pipeline_id=_PIPELINE_ID)

    assert changed == [agent_id]
    assert agent.agent_commands == ["a", "b", "c"]


async def test_sync_noop_when_multi_item_already_equal() -> None:
    """Re-saving an unchanged multi-item bound node is a no-op — the row
    already equals the node's full list, so items are NOT dropped."""
    agent_id = uuid.uuid4()
    agent = SimpleNamespace(id=agent_id, agent_commands=["a", "b", "c"])
    session = _session_returning([agent])
    nodes = [_node(uuid.uuid4(), agent_id=agent_id, agent_commands=["a", "b", "c"])]

    changed = await _sync_agent_row_commands(session, org_id=_ORG_ID, nodes=nodes, pipeline_id=_PIPELINE_ID)

    assert not changed
    assert agent.agent_commands == ["a", "b", "c"]


async def test_sync_detects_change_for_reordered_multi_item() -> None:
    """A reordered multi-item list is treated as a real change and synced."""
    agent_id = uuid.uuid4()
    agent = SimpleNamespace(id=agent_id, agent_commands=["a", "b"])
    session = _session_returning([agent])
    nodes = [_node(uuid.uuid4(), agent_id=agent_id, agent_commands=["b", "a"])]

    changed = await _sync_agent_row_commands(session, org_id=_ORG_ID, nodes=nodes, pipeline_id=_PIPELINE_ID)

    assert changed == [agent_id]
    assert agent.agent_commands == ["b", "a"]


async def test_sync_noop_without_bound_nodes() -> None:
    """No node carries a bound agent_commands -> no query at all."""
    session = _session_returning([])
    nodes = [_node(uuid.uuid4(), agent_id=None, agent_commands=["standalone"])]

    changed = await _sync_agent_row_commands(session, org_id=_ORG_ID, nodes=nodes, pipeline_id=_PIPELINE_ID)

    assert not changed
    session.execute.assert_not_awaited()


async def test_sync_only_writes_agent_commands() -> None:
    """FAR-2008: the sync is deliberately narrow — it writes ONLY
    ``agent_commands``.

    The other four Agent-authoritative fields (``token_budget``,
    ``prompt_template``, ``model_backend_id``, ``parameter_schema_id``) are not
    node-editable on the typed graph model, so a save/restore must never copy
    node values onto the shared Agent row. ``schema_profile`` is the sixth
    field, node-authoritative via ``setdefault``, and is outside this contract
    entirely (synced in neither direction). Widening the sync would let a
    rollback silently revert an Agent edit made after the snapshot was taken.
    """
    agent_id = uuid.uuid4()
    model_backend_id = uuid.uuid4()
    parameter_schema_id = uuid.uuid4()
    agent = SimpleNamespace(
        id=agent_id,
        agent_commands=["old-command"],
        token_budget=10,
        prompt_template="agent-prompt",
        model_backend_id=model_backend_id,
        parameter_schema_id=parameter_schema_id,
        schema_profile="tool-rich",
    )
    session = _session_returning([agent])
    node = _node(uuid.uuid4(), agent_id=agent_id, agent_commands=["new-command"])
    node["token_budget"] = 999
    node["prompt_template"] = "node-prompt"
    node["model_backend_id"] = str(uuid.uuid4())
    node["parameter_schema_id"] = str(uuid.uuid4())
    node["schema_profile"] = "node-profile"

    changed = await _sync_agent_row_commands(session, org_id=_ORG_ID, nodes=[node], pipeline_id=_PIPELINE_ID)

    assert changed == [agent_id]
    assert agent.agent_commands == ["new-command"]
    assert agent.token_budget == 10
    assert agent.prompt_template == "agent-prompt"
    assert agent.model_backend_id == model_backend_id
    assert agent.parameter_schema_id == parameter_schema_id
    assert agent.schema_profile == "tool-rich"


async def test_sync_log_line_carries_pipeline_attribution(caplog: pytest.LogCaptureFixture) -> None:
    """FAR-2008: the sync log line names the Agent, the organisation AND (when
    the caller knows it) the pipeline whose graph was written — a shared Agent
    row may be bound by many pipelines, so agent alone does not attribute the
    mutation."""
    caplog.set_level(logging.INFO, logger="modulo.core.agent_command_sync")
    agent_id = uuid.uuid4()
    agent = SimpleNamespace(id=agent_id, agent_commands=["old-command"])
    session = _session_returning([agent])
    nodes = [_node(uuid.uuid4(), agent_id=agent_id, agent_commands=["new-command"])]

    await _sync_agent_row_commands(session, org_id=_ORG_ID, nodes=nodes, pipeline_id=_PIPELINE_ID)

    records = [r for r in caplog.records if r.message == "pipeline.graph.agent_commands_synced"]
    assert len(records) == 1
    record = records[0]
    assert record.agent_id == str(agent_id)  # type: ignore[attr-defined]
    assert record.organisation_id == str(_ORG_ID)  # type: ignore[attr-defined]
    assert record.pipeline_id == str(_PIPELINE_ID)  # type: ignore[attr-defined]


def test_snapshot_materializes_synced_agent_commands() -> None:
    """(b) After the sync, snapshot materialization applies the UPDATED Agent
    row value to the bound node — the command the operator PATCHed is what
    runs. This is the shadow mechanism that made the stale-row bug silent."""
    agent = Agent(name="reviewer", prompt_template="p")
    agent.agent_commands = ["patched-command"]
    node = _node(uuid.uuid4(), agent_id=uuid.uuid4(), agent_commands=["patched-command"])

    _apply_agent_fields(node, agent)

    assert node["agent_commands"] == ["patched-command"]


def test_snapshot_shadow_overrides_node_command_with_agent_row() -> None:
    """The shadow itself: an out-of-step Agent row would override the node
    value at snapshot time — the exact mechanism behind the FAR-488 incident,
    and the reason the row must be synced on every graph write."""
    agent = Agent(name="reviewer", prompt_template="p")
    agent.agent_commands = ["stale-agent-row-command"]
    node = _node(uuid.uuid4(), agent_id=uuid.uuid4(), agent_commands=["fresh-node-command"])

    _apply_agent_fields(node, agent)

    assert node["agent_commands"] == ["stale-agent-row-command"]


def test_apply_agent_fields_ownership_contract() -> None:
    """FAR-2008: the snapshot overlay's field-ownership contract is pinned.

    Five fields are Agent-authoritative and are overwritten from the row
    (``token_budget``, ``prompt_template``, ``model_backend_id``,
    ``agent_commands``, ``parameter_schema_id``); ``schema_profile`` is
    node-authoritative, so an explicit node value is preserved and the Agent
    value is only a default (``setdefault``). A future edit that widens or
    narrows this without updating the writers' sync contract fails here.
    """
    agent = Agent(name="ownership", prompt_template="agent-prompt")
    agent.token_budget = 4242
    model_backend_id = uuid.uuid4()
    parameter_schema_id = uuid.uuid4()
    agent.model_backend_id = model_backend_id
    agent.agent_commands = ["agent-command"]
    agent.parameter_schema_id = parameter_schema_id
    agent.schema_profile = "agent-profile"
    node = _node(uuid.uuid4(), agent_id=uuid.uuid4(), agent_commands=["node-command"])
    node["token_budget"] = 1
    node["prompt_template"] = "node-prompt"
    node["model_backend_id"] = str(uuid.uuid4())
    node["parameter_schema_id"] = str(uuid.uuid4())
    node["schema_profile"] = "node-profile"

    _apply_agent_fields(node, agent)

    assert node["token_budget"] == 4242
    assert node["prompt_template"] == "agent-prompt"
    assert node["model_backend_id"] == str(model_backend_id)
    assert node["agent_commands"] == ["agent-command"]
    assert node["parameter_schema_id"] == str(parameter_schema_id)
    assert node["schema_profile"] == "node-profile"


def test_apply_agent_fields_defaults_schema_profile_from_the_agent() -> None:
    """The node-authoritative half of the contract: with no explicit node
    ``schema_profile``, the Agent row's value is used as the default."""
    agent = Agent(name="ownership", prompt_template="agent-prompt")
    agent.schema_profile = "agent-profile"
    node = _node(uuid.uuid4(), agent_id=uuid.uuid4())

    _apply_agent_fields(node, agent)

    assert node["schema_profile"] == "agent-profile"


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


@pytest.fixture
def client() -> TestClient:
    mock_session = configure_mock_session(AsyncMock())
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    mock_session.begin = MagicMock(return_value=begin_cm)

    async def override_session() -> AsyncMock:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="testuser",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)  # type: ignore[misc]
    app.dependency_overrides.clear()


def test_put_graph_endpoint_invokes_agent_commands_sync(client: TestClient) -> None:
    """Wiring: the PUT /graph endpoint calls the sync inside its transaction
    with the incoming node data."""
    node_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    nodes = [_node(node_id, agent_id=agent_id, agent_commands=["opencode run -- patched"])]
    schema_pins: list[dict[str, Any]] = []
    backend_pins: list[dict[str, Any]] = []
    validation = MagicMock()
    validation.issues = []

    with (
        patch("modulo.api.routes.pipelines.replace_pipeline_graph", return_value=(nodes, [])),
        patch("modulo.api.routes.pipelines._sync_agent_row_commands", new=AsyncMock(return_value=1)) as sync_mock,
        patch("modulo.api.routes.pipelines.GraphValidator.validate_definition", return_value=validation),
        patch("modulo.api.routes.pipelines._resolve_graph_references", return_value=(schema_pins, backend_pins)),
        patch("modulo.api.routes.pipelines.get_pipeline", return_value=MagicMock(owner_team_id=None)),
        patch("modulo.api.routes.pipelines.set_rls_org"),
        patch("modulo.api.routes.pipelines.set_rls_user_context"),
    ):
        resp = client.put(
            f"/api/v1/pipelines/{_PIPELINE_ID}/graph",
            json={"nodes": nodes, "edges": []},
        )

    assert resp.status_code == 200
    sync_mock.assert_awaited_once()
    assert sync_mock.await_args.kwargs["org_id"] == _ORG_ID
    synced_nodes = sync_mock.await_args.kwargs["nodes"]
    assert any(node.get("agent_commands") == ["opencode run -- patched"] for node in synced_nodes)


def test_put_graph_endpoint_syncs_agent_rows_before_the_graph_write(client: TestClient) -> None:
    """Ordering (FAR-1810 review): the Agent-row sync must run BEFORE the
    ``replace_pipeline_graph`` call that mints the FAR-1810 edit snapshot. The
    snapshot copy's ``_materialize_agent_fields`` overwrites bound nodes from
    the Agent row, so a sync that ran AFTER the write (the pre-fix order)
    recorded a snapshot pinned to the stale command — the very defect that
    FAR-488a exists to prevent. Fails on the pre-fix ordering."""
    node_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    nodes = [_node(node_id, agent_id=agent_id, agent_commands=["opencode run -- patched"])]
    validation = MagicMock()
    validation.issues = []
    order: list[str] = []

    async def _record_sync(*_args: Any, **_kwargs: Any) -> list[uuid.UUID]:
        order.append("sync")
        return [agent_id]

    async def _record_replace(*_args: Any, **_kwargs: Any) -> tuple[list[dict[str, Any]], list[Any]]:
        order.append("replace")
        return (nodes, [])

    with (
        patch("modulo.api.routes.pipelines._sync_agent_row_commands", new=_record_sync),
        patch("modulo.api.routes.pipelines.replace_pipeline_graph", new=_record_replace),
        patch("modulo.api.routes.pipelines.GraphValidator.validate_definition", return_value=validation),
        patch("modulo.api.routes.pipelines._resolve_graph_references", return_value=([], [])),
        patch("modulo.api.routes.pipelines.get_pipeline", return_value=MagicMock(owner_team_id=None)),
        patch("modulo.api.routes.pipelines.set_rls_org"),
        patch("modulo.api.routes.pipelines.set_rls_user_context"),
    ):
        resp = client.put(
            f"/api/v1/pipelines/{_PIPELINE_ID}/graph",
            json={"nodes": nodes, "edges": []},
        )

    assert resp.status_code == 200, resp.text
    assert order == ["sync", "replace"]


async def test_save_graph_syncs_agent_rows_before_the_graph_write() -> None:
    """Ordering (FAR-1810 review) for the node-conversion save path: the shared
    ``_save_graph`` helper must sync the bound Agent rows BEFORE the write that
    mints the edit snapshot. Same defect class as the PUT /graph endpoint."""
    from modulo.api.routes.pipelines import _save_graph

    order: list[str] = []

    async def _record_sync(*_args: Any, **_kwargs: Any) -> list[uuid.UUID]:
        order.append("sync")
        return []

    async def _record_replace(*_args: Any, **_kwargs: Any) -> tuple[list[dict[str, Any]], list[Any]]:
        order.append("replace")
        return ([], [])

    with (
        patch("modulo.api.routes.pipelines._sync_agent_row_commands", new=_record_sync),
        patch("modulo.api.routes.pipelines.replace_pipeline_graph", new=_record_replace),
    ):
        await _save_graph(
            AsyncMock(),
            _PIPELINE_ID,
            _ORG_ID,
            [],
            [],
            is_privileged=True,
            caller_type="rest",
            recording=GraphWriteRecording(record_snapshot=True),
        )

    assert order == ["sync", "replace"]


# ---------------------------------------------------------------------------
# FAR-220: the sync write is a gated Agent write path
# ---------------------------------------------------------------------------


async def test_sync_rejects_unpinned_git_ref_from_agent_type_node() -> None:
    """FAR-220 security regression: an ``agent``-type node (the node type the
    graph validator's git-content gate does NOT check) carrying an unpinned
    ``git+`` ref must NOT overwrite the bound Agent row. The sync fails closed
    with the typed GitContentRefError and the row keeps its old value — this
    test FAILS without the gate (the row is poisoned ungated)."""
    agent_id = uuid.uuid4()
    agent = SimpleNamespace(id=agent_id, agent_commands=["old-command"])
    session = _session_returning([agent])
    nodes = [_agent_node(uuid.uuid4(), agent_id=agent_id, agent_commands=[_UNPINNED_REF])]

    with pytest.raises(GitContentRefError):
        await _sync_agent_row_commands(session, org_id=_ORG_ID, nodes=nodes, pipeline_id=_PIPELINE_ID)

    assert agent.agent_commands == ["old-command"]
    session.execute.assert_not_awaited()


async def test_sync_rejects_unpinned_git_ref_before_row_read_even_when_equal() -> None:
    """An unpinned ref is invalid input regardless of the row's current value:
    a PATCH whose node command list already mirrors an (already-poisoned) row
    is still rejected, so the operator is pointed at the gated Agent save path
    instead of silently re-affirming poisoned data."""
    agent_id = uuid.uuid4()
    agent = SimpleNamespace(id=agent_id, agent_commands=[_UNPINNED_REF])
    session = _session_returning([agent])
    nodes = [_agent_node(uuid.uuid4(), agent_id=agent_id, agent_commands=[_UNPINNED_REF])]

    with pytest.raises(GitContentRefError):
        await _sync_agent_row_commands(session, org_id=_ORG_ID, nodes=nodes, pipeline_id=_PIPELINE_ID)

    session.execute.assert_not_awaited()


async def test_sync_allows_pinned_git_ref_from_agent_type_node() -> None:
    """The gate must not over-block: a single-item pinned ``git+`` ref syncs
    to the bound Agent row exactly like an inline command."""
    agent_id = uuid.uuid4()
    agent = SimpleNamespace(id=agent_id, agent_commands=["old-command"])
    session = _session_returning([agent])
    nodes = [_agent_node(uuid.uuid4(), agent_id=agent_id, agent_commands=[_PINNED_REF])]

    changed = await _sync_agent_row_commands(session, org_id=_ORG_ID, nodes=nodes, pipeline_id=_PIPELINE_ID)

    assert changed == [agent_id]
    assert agent.agent_commands == [_PINNED_REF]


def test_put_graph_endpoint_maps_unpinned_ref_sync_to_422(client: TestClient) -> None:
    """Wiring: when the Agent-row sync fails closed on an unpinned git content
    ref, the PUT /graph endpoint surfaces HTTP 422 (not the decorator's 500)
    and the enclosing transaction rolls back the graph write."""
    node_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    nodes = [_node(node_id, agent_id=agent_id, agent_commands=[_UNPINNED_REF])]
    schema_pins: list[dict[str, Any]] = []
    backend_pins: list[dict[str, Any]] = []
    validation = MagicMock()
    validation.issues = []
    sync_error = GitContentRefError("agent_commands[0] git content ref ... is not pinned to a commit SHA")

    with (
        patch("modulo.api.routes.pipelines.replace_pipeline_graph", return_value=(nodes, [])),
        patch("modulo.api.routes.pipelines._sync_agent_row_commands", new=AsyncMock(side_effect=sync_error)),
        patch("modulo.api.routes.pipelines.GraphValidator.validate_definition", return_value=validation),
        patch("modulo.api.routes.pipelines._resolve_graph_references", return_value=(schema_pins, backend_pins)),
        patch("modulo.api.routes.pipelines.get_pipeline", return_value=MagicMock(owner_team_id=None)),
        patch("modulo.api.routes.pipelines.set_rls_org"),
        patch("modulo.api.routes.pipelines.set_rls_user_context"),
    ):
        resp = client.put(
            f"/api/v1/pipelines/{_PIPELINE_ID}/graph",
            json={"nodes": nodes, "edges": []},
        )

    assert resp.status_code == 422
    assert "not pinned" in resp.json()["detail"]


def test_update_endpoint_maps_unpinned_ref_sync_to_422(client: TestClient) -> None:
    """Wiring: the graph_json-inside-PATCH-update path runs the same sync and
    maps its GitContentRefError to HTTP 422 — a declarative apply cannot ship
    an unpinned ref into an Agent row via this bypass either."""
    node_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    node = {"id": str(node_id), "agent_id": str(agent_id), "position": {"x": 0, "y": 0}}
    pipeline = MagicMock()
    pipeline.owner_team_id = None
    pipeline.graph_nodes_json = []
    sync_error = GitContentRefError("agent_commands[0] git content ref ... is not pinned to a commit SHA")

    with (
        patch("modulo.api.routes.pipelines._get_pipeline_or_404", new=AsyncMock(return_value=pipeline)),
        patch("modulo.api.routes.pipelines._assert_team_transition_allowed", new=AsyncMock()),
        patch("modulo.api.routes.pipelines._maybe_audit_autonomy_change", new=AsyncMock()),
        patch("modulo.api.routes.pipelines.get_pipeline", return_value=pipeline),
        patch("modulo.api.routes.pipelines.find_connector_team_mismatches", new=AsyncMock(return_value=[])),
        patch("modulo.api.routes.pipelines._resolve_graph_references", new=AsyncMock(return_value=([], []))),
        patch("modulo.api.routes.pipelines.replace_pipeline_graph", return_value=([node], [])),
        patch("modulo.api.routes.pipelines._sync_agent_row_commands", new=AsyncMock(side_effect=sync_error)),
        patch("modulo.api.routes.pipelines.update_pipeline", return_value=pipeline),
        patch("modulo.api.routes.pipelines.set_rls_org"),
        patch("modulo.api.routes.pipelines.set_rls_user_context"),
    ):
        resp = client.patch(
            f"/api/v1/pipelines/{_PIPELINE_ID}",
            json={"graph_json": {"nodes": [node], "edges": []}},
        )

    assert resp.status_code == 422
    assert "not pinned" in resp.json()["detail"]


async def test_finalize_locked_graph_save_maps_unpinned_ref_to_422() -> None:
    """The convert-to-agent / revert-to-manual shared mapping translates the
    sync's GitContentRefError into HTTP 422."""
    principal = AuthenticatedPrincipal(
        username="testuser",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )

    with pytest.raises(HTTPException) as exc_info:
        await _finalize_locked_graph_save(
            GitContentRefError("agent_commands[0] git content ref ... is not pinned to a commit SHA"),
            AsyncMock(),
            principal=principal,
            pipeline_id=_PIPELINE_ID,
        )

    assert exc_info.value.status_code == 422
    assert "not pinned" in exc_info.value.detail


# ---------------------------------------------------------------------------
# FAR-1845: the snapshot-rollback writer must sync the restored nodes'
# agent_commands into their bound Agent rows, inside the same transaction and
# BEFORE the graph write — "what you restore is what runs".
#
# The route's post-lock callback rides ``rollback_to_snapshot``'s
# ``post_lock_gate`` hook (FAR-1515), which the production function invokes
# under the pipeline row lock and BEFORE it rewrites ``graph_nodes_json``. The
# fake below reproduces that documented invocation point so the test pins the
# route's ordering (sync -> connector gate -> write) without standing up the
# whole snapshot machinery.
# ---------------------------------------------------------------------------


def _snapshot_for_response() -> MagicMock:
    """A stand-in the rollback response serialiser can render."""
    s = MagicMock()
    s.id = uuid.uuid4()
    s.pipeline_id = _PIPELINE_ID
    s.snapshot_version = 4
    s.tag = None
    s.notes = None
    s.created_at = datetime(2025, 1, 1, tzinfo=UTC)
    s.account_id = _USER_ID
    s.version_kind = "edit"
    s.created_kind = "rollback"
    s.draft = False
    s.channel = "none"
    s.environment_profile_id = None
    return s


def _locked_pipeline() -> MagicMock:
    p = MagicMock()
    p.organisation_id = _ORG_ID
    p.owner_team_id = None
    return p


async def test_rollback_endpoint_syncs_agent_rows_from_the_restored_snapshot(client: TestClient) -> None:
    """Wiring + ordering: the rollback post-lock gate runs the FAR-488a
    Agent-row sync with the TARGET snapshot's nodes (and the locked pipeline's
    org id) BEFORE the graph write, then still runs the connector-team gate.

    FAILS without the FAR-1845 change: the pre-change callback only ran the
    connector gate (and returned early when the graph had no connector
    bindings), so no Agent-row sync was ever invoked on rollback.
    """
    agent_id = uuid.uuid4()
    connector_id = uuid.uuid4()
    restored_node = _node(uuid.uuid4(), agent_id=agent_id, agent_commands=["opencode run -- restored"])
    restored_node["connector_binding"] = {"type": "github", "instance_id": str(connector_id)}
    target = MagicMock()
    target.graph_json = {"nodes": [restored_node], "edges": []}
    pipeline = _locked_pipeline()
    new_snapshot = _snapshot_for_response()

    order: list[str] = []
    sync_calls: list[dict[str, Any]] = []

    async def _fake_rollback(*_args: Any, **kwargs: Any) -> MagicMock:
        # Production ``rollback_to_snapshot`` invokes ``post_lock_gate`` under
        # the row lock and BEFORE it rewrites graph_nodes_json — reproduce that
        # call site exactly.
        order.append("rollback_start")
        await kwargs["post_lock_gate"](target, pipeline)
        order.append("graph_write")
        return new_snapshot

    async def _record_sync(*_args: Any, **kwargs: Any) -> list[uuid.UUID]:
        order.append("sync")
        sync_calls.append(kwargs)
        return [agent_id]

    async def _record_gate(*_args: Any, **_kwargs: Any) -> None:
        order.append("connector_gate")

    with (
        patch("modulo.api.routes.pipelines.rollback_to_snapshot", new=_fake_rollback),
        patch("modulo.api.routes.pipelines._sync_agent_row_commands", new=_record_sync),
        patch("modulo.api.routes.pipelines._enforce_connector_team_bindings", new=_record_gate),
        patch("modulo.api.routes.pipelines.append_audit_event", new=AsyncMock()),
    ):
        resp = client.post(f"/api/v1/pipelines/{_PIPELINE_ID}/snapshots/{_SNAP_ID}/rollback")

    assert resp.status_code == 200, resp.text
    assert order == ["rollback_start", "sync", "connector_gate", "graph_write"], order
    assert len(sync_calls) == 1
    assert sync_calls[0]["org_id"] == _ORG_ID
    assert sync_calls[0]["pipeline_id"] == _PIPELINE_ID
    assert sync_calls[0]["nodes"] == target.graph_json["nodes"]


async def test_rollback_endpoint_syncs_even_without_connector_bindings(client: TestClient) -> None:
    """The Agent-row sync is NOT behind the connector gate's early return: a
    restored graph with no connector bindings still syncs (the pre-change hook
    returned before doing anything when there were no bindings)."""
    agent_id = uuid.uuid4()
    restored_node = _node(uuid.uuid4(), agent_id=agent_id, agent_commands=["opencode run -- restored"])
    target = MagicMock()
    target.graph_json = {"nodes": [restored_node], "edges": []}
    pipeline = _locked_pipeline()

    sync_calls: list[dict[str, Any]] = []

    async def _fake_rollback(*_args: Any, **kwargs: Any) -> MagicMock:
        await kwargs["post_lock_gate"](target, pipeline)
        return _snapshot_for_response()

    async def _record_sync(*_args: Any, **kwargs: Any) -> list[uuid.UUID]:
        sync_calls.append(kwargs)
        return [agent_id]

    gate_mock = AsyncMock()
    with (
        patch("modulo.api.routes.pipelines.rollback_to_snapshot", new=_fake_rollback),
        patch("modulo.api.routes.pipelines._sync_agent_row_commands", new=_record_sync),
        patch("modulo.api.routes.pipelines._enforce_connector_team_bindings", new=gate_mock),
        patch("modulo.api.routes.pipelines.append_audit_event", new=AsyncMock()),
    ):
        resp = client.post(f"/api/v1/pipelines/{_PIPELINE_ID}/snapshots/{_SNAP_ID}/rollback")

    assert resp.status_code == 200, resp.text
    assert len(sync_calls) == 1
    assert sync_calls[0]["pipeline_id"] == _PIPELINE_ID
    assert sync_calls[0]["nodes"] == target.graph_json["nodes"]
    gate_mock.assert_not_awaited()


async def test_rollback_endpoint_audits_agent_command_sync(client: TestClient) -> None:
    """FAR-2008 attribution: when the rollback's sync actually rewrites Agent
    rows, a durable ``pipeline.agent_commands_synced`` event is appended in the
    SAME transaction (the route transaction), naming the pipeline, the actor
    and every changed Agent row id.

    The structured log line alone is not enough: a rollback can rewrite a
    SHARED Agent row (one Agent bound by several pipelines), so the mutation
    must be attributable in the audit chain.
    """
    agent_id = uuid.uuid4()
    restored_node = _node(uuid.uuid4(), agent_id=agent_id, agent_commands=["opencode run -- restored"])
    target = MagicMock()
    target.graph_json = {"nodes": [restored_node], "edges": []}
    pipeline = _locked_pipeline()

    async def _fake_rollback(*_args: Any, **kwargs: Any) -> MagicMock:
        await kwargs["post_lock_gate"](target, pipeline)
        return _snapshot_for_response()

    audit = AsyncMock()
    with (
        patch("modulo.api.routes.pipelines.rollback_to_snapshot", new=_fake_rollback),
        patch("modulo.api.routes.pipelines._sync_agent_row_commands", new=AsyncMock(return_value=[agent_id])),
        patch("modulo.api.routes.pipelines.append_audit_event", new=audit),
    ):
        resp = client.post(f"/api/v1/pipelines/{_PIPELINE_ID}/snapshots/{_SNAP_ID}/rollback")

    assert resp.status_code == 200, resp.text
    audit.assert_awaited_once()
    kwargs = audit.await_args.kwargs
    assert kwargs["org_id"] == _ORG_ID
    assert kwargs["event_type"] == "pipeline.agent_commands_synced"
    assert kwargs["actor_user_id"] == _USER_ID
    assert kwargs["resource_type"] == "pipeline"
    assert kwargs["resource_id"] == _PIPELINE_ID
    assert kwargs["payload_json"] == {"agent_count": 1, "agent_ids": [str(agent_id)]}


async def test_rollback_endpoint_does_not_audit_a_noop_sync(client: TestClient) -> None:
    """A rollback whose restored commands already match every bound Agent row
    changes nothing, so it must NOT append an agent-sync audit event (only a
    real mutation is recorded)."""
    restored_node = _node(uuid.uuid4(), agent_id=uuid.uuid4(), agent_commands=["opencode run -- same"])
    target = MagicMock()
    target.graph_json = {"nodes": [restored_node], "edges": []}
    pipeline = _locked_pipeline()

    async def _fake_rollback(*_args: Any, **kwargs: Any) -> MagicMock:
        await kwargs["post_lock_gate"](target, pipeline)
        return _snapshot_for_response()

    audit = AsyncMock()
    with (
        patch("modulo.api.routes.pipelines.rollback_to_snapshot", new=_fake_rollback),
        patch("modulo.api.routes.pipelines._sync_agent_row_commands", new=AsyncMock(return_value=[])),
        patch("modulo.api.routes.pipelines.append_audit_event", new=audit),
    ):
        resp = client.post(f"/api/v1/pipelines/{_PIPELINE_ID}/snapshots/{_SNAP_ID}/rollback")

    assert resp.status_code == 200, resp.text
    audit.assert_not_awaited()


async def test_rollback_endpoint_maps_unpinned_ref_sync_to_422(client: TestClient) -> None:
    """A poisoned (unpinned ``git+``) ref in the restored snapshot fails the
    rollback closed: the sync's ``GitContentRefError`` maps to HTTP 422 (the
    same mapping every graph-write endpoint uses), never the decorator's 500.
    Mirrors ``test_put_graph_endpoint_maps_unpinned_ref_sync_to_422``."""
    agent_id = uuid.uuid4()
    restored_node = _node(uuid.uuid4(), agent_id=agent_id, agent_commands=[_UNPINNED_REF])
    target = MagicMock()
    target.graph_json = {"nodes": [restored_node], "edges": []}
    pipeline = _locked_pipeline()
    sync_error = GitContentRefError("agent_commands[0] git content ref ... is not pinned to a commit SHA")

    async def _fake_rollback(*_args: Any, **kwargs: Any) -> MagicMock:
        await kwargs["post_lock_gate"](target, pipeline)
        return _snapshot_for_response()

    with (
        patch("modulo.api.routes.pipelines.rollback_to_snapshot", new=_fake_rollback),
        patch("modulo.api.routes.pipelines._sync_agent_row_commands", new=AsyncMock(side_effect=sync_error)),
    ):
        resp = client.post(f"/api/v1/pipelines/{_PIPELINE_ID}/snapshots/{_SNAP_ID}/rollback")

    assert resp.status_code == 422
    assert "not pinned" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# FAR-1845: per-writer coverage for the MCP full-graph writer.
#
# The MCP write already syncs (FAR-1810 / PR #1848) but had no test pinning it;
# the ordering matters for the same reason as the REST path — the write mints
# the edit snapshot whose ``_materialize_agent_fields`` overwrites bound nodes
# from the Agent row, so a sync that ran after the write would pin the stale
# command into the snapshot.
# ---------------------------------------------------------------------------


async def test_mcp_graph_writer_syncs_agent_rows_before_the_graph_write() -> None:
    """The MCP ``_replace_pipeline_graph_txn`` writer takes the pipeline row
    lock BEFORE the FAR-488a Agent-row sync, which in turn runs BEFORE
    ``replace_pipeline_graph`` stamps the edit snapshot.

    FAR-1845: the standardised order is Pipeline -> Agent. Syncing first (which
    UPDATEs Agent rows, flushed by autoflush) and only then taking the pipeline
    ``FOR UPDATE`` would acquire Agent -> Pipeline and could deadlock a
    concurrent graph write or snapshot rollback sharing an Agent.
    """
    order: list[str] = []
    nodes = [_node(uuid.uuid4(), agent_id=uuid.uuid4(), agent_commands=["opencode run -- mcp"])]
    pipeline = MagicMock()
    pipeline.owner_team_id = None
    pipeline.graph_nodes_json = []

    lock_statements: list[Any] = []

    class _LockResult:
        def scalar_one_or_none(self) -> Any:
            return pipeline

    async def _record_pipeline_lock(stmt: Any, *_args: Any, **_kwargs: Any) -> _LockResult:
        # FAR-1845: capture the statement, not just the ordering. Without this
        # the test still passes if the lock degrades to a plain ``select``.
        lock_statements.append(stmt)
        order.append("lock")
        return _LockResult()

    async def _record_sync(*_args: Any, **_kwargs: Any) -> list[uuid.UUID]:
        order.append("sync")
        return []

    async def _record_replace(*_args: Any, **_kwargs: Any) -> tuple[list[Any], list[Any]]:
        order.append("replace")
        return (nodes, [])

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=_record_pipeline_lock)

    @asynccontextmanager
    async def _fake_session(_org_id: uuid.UUID):
        yield session

    with (
        patch.object(ms, "_session", _fake_session),
        patch.object(ms, "_reject_unsupported_dispatch_bindings", new=AsyncMock(return_value=None)),
        patch.object(ms, "_team_scoped_key_mismatch", return_value=False),
        patch.object(ms, "merge_masked_graph_nodes", new=lambda incoming, _stored: incoming),
        patch("modulo.db.crud.row_lock.set_mutation_row_lock_timeout", new=AsyncMock()),
        patch("modulo.db.crud.pipeline.get_pipeline", new=AsyncMock(return_value=pipeline)),
        patch("modulo.db.crud.pipeline.replace_pipeline_graph", new=_record_replace),
        patch("modulo.core.team_visibility.find_connector_team_mismatches", new=AsyncMock(return_value=[])),
        patch("modulo.core.agent_command_sync.sync_agent_row_commands", new=_record_sync),
    ):
        result = await ms._replace_pipeline_graph_txn(
            _ORG_ID,
            uuid.uuid4(),
            str(uuid.uuid4()),
            nodes,
            [],
            is_privileged=True,
            graph_account_id=_USER_ID,
            is_guardrail_admin=False,
        )

    assert order == ["lock", "sync", "replace"], order
    assert len(lock_statements) == 1, lock_statements
    lock_stmt = lock_statements[0]
    assert lock_stmt._for_update_arg is not None  # type: ignore[attr-defined]
    assert "FROM pipelines" in str(lock_stmt)
    assert "FOR UPDATE" in str(lock_stmt)
    assert result[0] == nodes
