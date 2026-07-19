"""Unit tests for GraphValidator environment capability checks."""

import uuid
from unittest.mock import AsyncMock, MagicMock

from modulo.core.graph_validator import GraphValidator

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_session():
    """Return an AsyncMock session with sensible defaults."""
    return AsyncMock()


def _mock_agent(
    agent_id: uuid.UUID | None = None,
    *,
    name: str = "test-agent",
    required_capabilities: list[str] | None = None,
) -> MagicMock:
    agent = MagicMock()
    agent.id = agent_id or uuid.uuid4()
    agent.name = name
    agent.required_environment_capabilities = required_capabilities or []
    return agent


def _mock_profile(
    profile_id: uuid.UUID | None = None,
    *,
    name: str = "test-profile",
    capabilities: list[str] | None = None,
) -> MagicMock:
    profile = MagicMock()
    profile.id = profile_id or uuid.uuid4()
    profile.name = name
    profile.capabilities_json = capabilities or []
    return profile


def _graph_json(*, agent_ids: list[uuid.UUID] | None = None) -> dict:
    nodes = []
    for aid in agent_ids or []:
        nodes.append({"id": str(uuid.uuid4()), "agent_id": str(aid)})
    return {"nodes": nodes, "edges": []}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


_VALID_SINGLE_NODE = {"nodes": [{"id": "n1"}], "edges": []}


async def test_no_env_profile_skips_check():
    """If environment_profile_id is None, the check is skipped."""
    validator = GraphValidator()
    session = _mock_session()
    result = await validator.validate_definition(
        _VALID_SINGLE_NODE,
        session,
        environment_profile_id=None,
    )
    assert result.is_valid
    session.execute.assert_not_called()


async def test_missing_profile_is_error():
    """If environment_profile_id is set but profile not found, emit error."""
    pid = uuid.uuid4()
    session = _mock_session()
    session.get = AsyncMock(return_value=None)

    validator = GraphValidator()
    result = await validator.validate_definition(
        _VALID_SINGLE_NODE,
        session,
        environment_profile_id=pid,
    )
    assert not result.is_valid
    assert any(i.code == "ENV_PROFILE_NOT_FOUND" for i in result.issues)


async def test_no_agent_ids_skips_agent_check():
    """If graph has no agent_id references, no DB query for agents."""
    pid = uuid.uuid4()
    profile = _mock_profile(pid, capabilities=["docker"])

    session = _mock_session()
    session.get = AsyncMock(return_value=profile)

    validator = GraphValidator()
    result = await validator.validate_definition(
        {"nodes": [{"id": "n1"}], "edges": []},  # no agent_id
        session,
        environment_profile_id=pid,
    )
    assert result.is_valid
    session.execute.assert_not_called()  # no agent fetch


async def test_empty_required_capabilities_is_valid():
    """If agents have no required capabilities, no error."""
    pid = uuid.uuid4()
    aid = uuid.uuid4()
    profile = _mock_profile(pid, capabilities=["docker"])
    agent = _mock_agent(aid, name="agent-a", required_capabilities=[])

    session = _mock_session()
    session.get = AsyncMock(return_value=profile)
    scalars_result = MagicMock()
    scalars_result.all.return_value = [agent]
    execute_result = MagicMock()
    execute_result.scalars.return_value = scalars_result
    session.execute = AsyncMock(return_value=execute_result)

    validator = GraphValidator()
    result = await validator.validate_definition(
        _graph_json(agent_ids=[aid]),
        session,
        environment_profile_id=pid,
    )
    assert result.is_valid


async def test_agent_capabilities_satisfied():
    """If all agent capabilities are in the profile, it's valid."""
    pid = uuid.uuid4()
    aid = uuid.uuid4()
    profile = _mock_profile(pid, capabilities=["docker", "gpu", "network"])
    agent = _mock_agent(aid, name="agent-a", required_capabilities=["docker", "network"])

    session = _mock_session()
    session.get = AsyncMock(return_value=profile)
    scalars_result = MagicMock()
    scalars_result.all.return_value = [agent]
    execute_result = MagicMock()
    execute_result.scalars.return_value = scalars_result
    session.execute = AsyncMock(return_value=execute_result)

    validator = GraphValidator()
    result = await validator.validate_definition(
        _graph_json(agent_ids=[aid]),
        session,
        environment_profile_id=pid,
    )
    assert result.is_valid


async def test_agent_capabilities_missing_is_error():
    """If any agent requires a capability the profile lacks, it's an error."""
    pid = uuid.uuid4()
    aid = uuid.uuid4()
    profile = _mock_profile(pid, capabilities=["docker"])
    agent = _mock_agent(aid, name="agent-a", required_capabilities=["docker", "gpu", "network"])

    session = _mock_session()
    session.get = AsyncMock(return_value=profile)
    scalars_result = MagicMock()
    scalars_result.all.return_value = [agent]
    execute_result = MagicMock()
    execute_result.scalars.return_value = scalars_result
    session.execute = AsyncMock(return_value=execute_result)

    validator = GraphValidator()
    result = await validator.validate_definition(
        _graph_json(agent_ids=[aid]),
        session,
        environment_profile_id=pid,
    )
    assert not result.is_valid
    assert any(i.code == "ENV_MISSING_CAPABILITIES" for i in result.issues)


async def test_multiple_agents_all_checked():
    """Multiple agents are all checked against the same profile."""
    pid = uuid.uuid4()
    aid1 = uuid.uuid4()
    aid2 = uuid.uuid4()
    profile = _mock_profile(pid, capabilities=["docker"])
    agent1 = _mock_agent(aid1, name="agent-a", required_capabilities=["docker"])
    agent2 = _mock_agent(aid2, name="agent-b", required_capabilities=["gpu"])

    session = _mock_session()
    session.get = AsyncMock(return_value=profile)
    scalars_result = MagicMock()
    scalars_result.all.return_value = [agent1, agent2]
    execute_result = MagicMock()
    execute_result.scalars.return_value = scalars_result
    session.execute = AsyncMock(return_value=execute_result)

    validator = GraphValidator()
    result = await validator.validate_definition(
        _graph_json(agent_ids=[aid1, aid2]),
        session,
        environment_profile_id=pid,
    )
    assert not result.is_valid
    errors = [i for i in result.issues if i.code == "ENV_MISSING_CAPABILITIES"]
    assert len(errors) == 1  # only agent-b is missing


async def test_validate_for_run_includes_env_check():
    """validate_for_run also runs the environment capability check."""
    pid = uuid.uuid4()
    aid = uuid.uuid4()
    profile = _mock_profile(pid, capabilities=["docker"])
    agent = _mock_agent(aid, name="agent-a", required_capabilities=["gpu"])

    session = _mock_session()
    session.get = AsyncMock(return_value=profile)
    scalars_result = MagicMock()
    scalars_result.all.return_value = [agent]
    execute_result = MagicMock()
    execute_result.scalars.return_value = scalars_result
    session.execute = AsyncMock(return_value=execute_result)

    snap = MagicMock()
    snap.graph_json = _graph_json(agent_ids=[aid])
    snap.schema_pins_json = []
    snap.connector_bindings_json = []
    snap.model_backend_pins_json = []
    snap.environment_profile_id = pid

    validator = GraphValidator()
    result = await validator.validate_for_run(snap, {}, session)
    assert not result.is_valid
    assert any(i.code == "ENV_MISSING_CAPABILITIES" for i in result.issues)


async def test_validate_includes_env_check():
    """Snapshot validate() also runs the environment capability check."""
    pid = uuid.uuid4()
    aid = uuid.uuid4()
    profile = _mock_profile(pid, capabilities=["docker"])
    agent = _mock_agent(aid, name="agent-a", required_capabilities=["gpu"])

    session = _mock_session()
    session.get = AsyncMock(return_value=profile)
    scalars_result = MagicMock()
    scalars_result.all.return_value = [agent]
    execute_result = MagicMock()
    execute_result.scalars.return_value = scalars_result
    session.execute = AsyncMock(return_value=execute_result)

    snap = MagicMock()
    snap.graph_json = _graph_json(agent_ids=[aid])
    snap.schema_pins_json = []
    snap.connector_bindings_json = []
    snap.model_backend_pins_json = []
    snap.environment_profile_id = pid

    validator = GraphValidator()
    result = await validator.validate(snap, session)
    assert not result.is_valid
    assert any(i.code == "ENV_MISSING_CAPABILITIES" for i in result.issues)


async def test_non_uuid_agent_id_skipped():
    """If a node has an invalid agent_id, it's skipped without error."""
    pid = uuid.uuid4()
    profile = _mock_profile(pid, capabilities=["docker"])

    session = _mock_session()
    session.get = AsyncMock(return_value=profile)
    # No agents should be fetched
    scalars_result = MagicMock()
    scalars_result.all.return_value = []
    execute_result = MagicMock()
    execute_result.scalars.return_value = scalars_result
    session.execute = AsyncMock(return_value=execute_result)

    graph = {
        "nodes": [{"id": "n1", "agent_id": "not-a-uuid"}],
        "edges": [],
    }

    validator = GraphValidator()
    result = await validator.validate_definition(
        graph,
        session,
        environment_profile_id=pid,
    )
    assert result.is_valid
