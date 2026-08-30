"""FAR-488a: node-level agent_command PATCHes must reach the bound Agent row.

At snapshot time ``_apply_agent_fields`` overwrites a bound node's
``agent_command`` with the Agent row's non-NULL value. The graph PATCH used to
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
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context, get_settings
from modulo.api.main import app
from modulo.api.routes.pipelines import _extract_agent_command_sync_updates, _sync_agent_row_commands
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.db.crud.pipeline_snapshot import _apply_agent_fields
from modulo.db.models.agent import Agent
from modulo.settings import Settings
from tests.unit.api.mock_session import configure_mock_session

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_PIPELINE_ID = uuid.uuid4()


def _node(node_id: uuid.UUID, *, agent_id: uuid.UUID | None, agent_command: str | None = None) -> dict[str, Any]:
    node: dict[str, Any] = {
        "id": str(node_id),
        "position": {"x": 0, "y": 0},
        "connector_binding": None,
    }
    if agent_id is not None:
        node["agent_id"] = str(agent_id)
    if agent_command is not None:
        node["agent_command"] = agent_command
    return node


def test_extract_updates_first_command_per_distinct_agent() -> None:
    """Two nodes bound to the same agent yield ONE update (first node wins);
    distinct agents each yield their own."""
    agent_a = uuid.uuid4()
    agent_b = uuid.uuid4()
    nodes = [
        _node(uuid.uuid4(), agent_id=agent_a, agent_command="first"),
        _node(uuid.uuid4(), agent_id=agent_a, agent_command="second"),
        _node(uuid.uuid4(), agent_id=agent_b, agent_command="other"),
    ]
    updates = _extract_agent_command_sync_updates(nodes)
    assert updates == {agent_a: "first", agent_b: "other"}


def test_extract_updates_skips_unbound_and_commandless_nodes() -> None:
    """Nodes without an agent_id, without an agent_command, with an empty
    command, or with an unparseable agent_id are all skipped."""
    nodes = [
        _node(uuid.uuid4(), agent_id=None, agent_command="orphan"),
        _node(uuid.uuid4(), agent_id=uuid.uuid4()),
        _node(uuid.uuid4(), agent_id=uuid.uuid4(), agent_command=""),
        _node(uuid.uuid4(), agent_id=None),
        {"id": str(uuid.uuid4()), "position": {"x": 0, "y": 0}, "agent_id": "not-a-uuid", "agent_command": "x"},
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
    agent = SimpleNamespace(id=agent_id, agent_command="old-command")
    session = _session_returning([agent])
    nodes = [_node(uuid.uuid4(), agent_id=agent_id, agent_command="new-command")]

    changed = await _sync_agent_row_commands(session, org_id=_ORG_ID, nodes=nodes)

    assert changed == 1
    assert agent.agent_command == "new-command"


async def test_sync_skips_agent_row_with_null_command() -> None:
    """(d) An Agent row with a NULL agent_command is NOT updated â€” the node
    value already stands at snapshot time."""
    agent_id = uuid.uuid4()
    agent = SimpleNamespace(id=agent_id, agent_command=None)
    session = _session_returning([agent])
    nodes = [_node(uuid.uuid4(), agent_id=agent_id, agent_command="node-command")]

    changed = await _sync_agent_row_commands(session, org_id=_ORG_ID, nodes=nodes)

    assert changed == 0
    assert agent.agent_command is None


async def test_sync_noop_when_command_already_equal() -> None:
    agent_id = uuid.uuid4()
    agent = SimpleNamespace(id=agent_id, agent_command="same")
    session = _session_returning([agent])
    nodes = [_node(uuid.uuid4(), agent_id=agent_id, agent_command="same")]

    changed = await _sync_agent_row_commands(session, org_id=_ORG_ID, nodes=nodes)

    assert changed == 0


async def test_sync_noop_without_bound_nodes() -> None:
    """No node carries a bound agent_command -> no query at all."""
    session = _session_returning([])
    nodes = [_node(uuid.uuid4(), agent_id=None, agent_command="standalone")]

    changed = await _sync_agent_row_commands(session, org_id=_ORG_ID, nodes=nodes)

    assert changed == 0
    session.execute.assert_not_awaited()


def test_snapshot_materializes_synced_agent_command() -> None:
    """(b) After the sync, snapshot materialization applies the UPDATED Agent
    row value to the bound node â€” the command the operator PATCHed is what
    runs. This is the shadow mechanism that made the stale-row bug silent."""
    agent = Agent(name="reviewer", prompt_template="p")
    agent.agent_command = "patched-command"
    node = _node(uuid.uuid4(), agent_id=uuid.uuid4(), agent_command="patched-command")

    _apply_agent_fields(node, agent)

    assert node["agent_command"] == "patched-command"


def test_snapshot_shadow_overrides_node_command_with_agent_row() -> None:
    """The shadow itself: an out-of-step Agent row would override the node
    value at snapshot time â€” the exact mechanism behind the FAR-488 incident,
    and the reason the row must be synced on every graph PATCH."""
    agent = Agent(name="reviewer", prompt_template="p")
    agent.agent_command = "stale-agent-row-command"
    node = _node(uuid.uuid4(), agent_id=uuid.uuid4(), agent_command="fresh-node-command")

    _apply_agent_fields(node, agent)

    assert node["agent_command"] == "stale-agent-row-command"


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


def test_patch_graph_endpoint_invokes_agent_command_sync(client: TestClient) -> None:
    """Wiring: the PATCH /graph endpoint calls the sync inside its transaction
    with the incoming node data."""
    node_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    nodes = [_node(node_id, agent_id=agent_id, agent_command="opencode run -- patched")]
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
    assert any(node.get("agent_command") == "opencode run -- patched" for node in synced_nodes)
