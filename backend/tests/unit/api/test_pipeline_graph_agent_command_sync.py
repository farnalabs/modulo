"""FAR-488a: node-level agent_commands PATCHes must reach the bound Agent row.

At snapshot time ``_apply_agent_fields`` overwrites a bound node's
``agent_commands`` with the Agent row's non-NULL value. The graph PATCH used to
persist node-level commands to ``graph_nodes_json`` only, so an operator's
PATCH read back correctly but every run silently executed the stale Agent-row
command. These tests pin the sync ("what you PATCH is what runs") at three
levels: the pure extractor, the DB sync helper against a mocked session, and
the PATCH /graph endpoint wiring.
"""

import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

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
from modulo.db.crud.pipeline_snapshot import _apply_agent_fields
from modulo.db.models.agent import Agent
from modulo.settings import Settings
from tests.unit.api.mock_session import configure_mock_session

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_PIPELINE_ID = uuid.uuid4()

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

    changed = await _sync_agent_row_commands(session, org_id=_ORG_ID, nodes=nodes)

    assert changed == 1
    assert agent.agent_commands == ["new-command"]


async def test_sync_skips_agent_row_with_null_command() -> None:
    """(d) An Agent row with a NULL agent_commands is NOT updated — the node
    value already stands at snapshot time."""
    agent_id = uuid.uuid4()
    agent = SimpleNamespace(id=agent_id, agent_commands=None)
    session = _session_returning([agent])
    nodes = [_node(uuid.uuid4(), agent_id=agent_id, agent_commands=["node-command"])]

    changed = await _sync_agent_row_commands(session, org_id=_ORG_ID, nodes=nodes)

    assert changed == 0
    assert agent.agent_commands is None


async def test_sync_noop_when_command_already_equal() -> None:
    agent_id = uuid.uuid4()
    agent = SimpleNamespace(id=agent_id, agent_commands=["same"])
    session = _session_returning([agent])
    nodes = [_node(uuid.uuid4(), agent_id=agent_id, agent_commands=["same"])]

    changed = await _sync_agent_row_commands(session, org_id=_ORG_ID, nodes=nodes)

    assert changed == 0


async def test_sync_preserves_multi_item_list() -> None:
    """A PATCHed multi-item node command list is synced to the Agent row in
    full — no truncation to a single item (FAR-488-class silent divergence)."""
    agent_id = uuid.uuid4()
    agent = SimpleNamespace(id=agent_id, agent_commands=["old-command"])
    session = _session_returning([agent])
    nodes = [_node(uuid.uuid4(), agent_id=agent_id, agent_commands=["a", "b", "c"])]

    changed = await _sync_agent_row_commands(session, org_id=_ORG_ID, nodes=nodes)

    assert changed == 1
    assert agent.agent_commands == ["a", "b", "c"]


async def test_sync_noop_when_multi_item_already_equal() -> None:
    """Re-saving an unchanged multi-item bound node is a no-op — the row
    already equals the node's full list, so items are NOT dropped."""
    agent_id = uuid.uuid4()
    agent = SimpleNamespace(id=agent_id, agent_commands=["a", "b", "c"])
    session = _session_returning([agent])
    nodes = [_node(uuid.uuid4(), agent_id=agent_id, agent_commands=["a", "b", "c"])]

    changed = await _sync_agent_row_commands(session, org_id=_ORG_ID, nodes=nodes)

    assert changed == 0
    assert agent.agent_commands == ["a", "b", "c"]


async def test_sync_detects_change_for_reordered_multi_item() -> None:
    """A reordered multi-item list is treated as a real change and synced."""
    agent_id = uuid.uuid4()
    agent = SimpleNamespace(id=agent_id, agent_commands=["a", "b"])
    session = _session_returning([agent])
    nodes = [_node(uuid.uuid4(), agent_id=agent_id, agent_commands=["b", "a"])]

    changed = await _sync_agent_row_commands(session, org_id=_ORG_ID, nodes=nodes)

    assert changed == 1
    assert agent.agent_commands == ["b", "a"]


async def test_sync_noop_without_bound_nodes() -> None:
    """No node carries a bound agent_commands -> no query at all."""
    session = _session_returning([])
    nodes = [_node(uuid.uuid4(), agent_id=None, agent_commands=["standalone"])]

    changed = await _sync_agent_row_commands(session, org_id=_ORG_ID, nodes=nodes)

    assert changed == 0
    session.execute.assert_not_awaited()


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
    and the reason the row must be synced on every graph PATCH."""
    agent = Agent(name="reviewer", prompt_template="p")
    agent.agent_commands = ["stale-agent-row-command"]
    node = _node(uuid.uuid4(), agent_id=uuid.uuid4(), agent_commands=["fresh-node-command"])

    _apply_agent_fields(node, agent)

    assert node["agent_commands"] == ["stale-agent-row-command"]


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


def test_patch_graph_endpoint_invokes_agent_commands_sync(client: TestClient) -> None:
    """Wiring: the PATCH /graph endpoint calls the sync inside its transaction
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
        resp = client.patch(
            f"/api/v1/pipelines/{_PIPELINE_ID}/graph",
            json={"nodes": nodes, "edges": []},
        )

    assert resp.status_code == 200
    sync_mock.assert_awaited_once()
    assert sync_mock.await_args.kwargs["org_id"] == _ORG_ID
    synced_nodes = sync_mock.await_args.kwargs["nodes"]
    assert any(node.get("agent_commands") == ["opencode run -- patched"] for node in synced_nodes)


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
        await _sync_agent_row_commands(session, org_id=_ORG_ID, nodes=nodes)

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
        await _sync_agent_row_commands(session, org_id=_ORG_ID, nodes=nodes)

    session.execute.assert_not_awaited()


async def test_sync_allows_pinned_git_ref_from_agent_type_node() -> None:
    """The gate must not over-block: a single-item pinned ``git+`` ref syncs
    to the bound Agent row exactly like an inline command."""
    agent_id = uuid.uuid4()
    agent = SimpleNamespace(id=agent_id, agent_commands=["old-command"])
    session = _session_returning([agent])
    nodes = [_agent_node(uuid.uuid4(), agent_id=agent_id, agent_commands=[_PINNED_REF])]

    changed = await _sync_agent_row_commands(session, org_id=_ORG_ID, nodes=nodes)

    assert changed == 1
    assert agent.agent_commands == [_PINNED_REF]


def test_patch_graph_endpoint_maps_unpinned_ref_sync_to_422(client: TestClient) -> None:
    """Wiring: when the Agent-row sync fails closed on an unpinned git content
    ref, the PATCH /graph endpoint surfaces HTTP 422 (not the decorator's 500)
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
        resp = client.patch(
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
