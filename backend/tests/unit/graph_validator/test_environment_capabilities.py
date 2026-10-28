"""Unit tests for GraphValidator environment capability checks."""

import uuid
from unittest.mock import AsyncMock, MagicMock

from modulo.core.graph_validator import GraphValidator
from tests.unit.graph_validator._helpers import (
    session_returning,
)

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
    max_node_seconds: int = 3300,
) -> MagicMock:
    profile = MagicMock()
    profile.id = profile_id or uuid.uuid4()
    profile.name = name
    profile.capabilities_json = capabilities or []
    profile.max_node_seconds = max_node_seconds
    return profile


def _session_with_profile(profile: MagicMock | None, agents: list[MagicMock]) -> AsyncMock:
    """Mock session resolving *profile* via ``get()`` and *agents* via ``execute()``."""
    session = session_returning(agents)
    session.get = AsyncMock(return_value=profile)
    return session


def _graph_json(*, agent_ids: list[uuid.UUID] | None = None) -> dict:
    nodes = [{"id": str(uuid.uuid4()), "agent_id": str(aid)} for aid in agent_ids or []]
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


async def test_no_agent_ids_still_checks_profile_timeout_cap():
    """A sandbox without an Agent row still uses the selected profile cap."""
    pid = uuid.uuid4()
    profile = _mock_profile(pid, capabilities=["docker"], max_node_seconds=300)

    session = _mock_session()
    session.get = AsyncMock(return_value=profile)

    validator = GraphValidator()
    result = await validator.validate_definition(
        {
            "nodes": [{"id": "n1", "node_type": "sandbox_agent", "timeout_seconds": 301}],
            "edges": [],
        },
        session,
        environment_profile_id=pid,
    )
    assert not result.is_valid
    assert any(i.code == "SANDBOX_TIMEOUT_EXCEEDS_PROFILE_CAP" for i in result.issues)
    session.execute.assert_not_called()  # no agent fetch


async def test_lower_profile_cap_rejects_higher_node_timeout():
    pid = uuid.uuid4()
    profile = _mock_profile(pid, max_node_seconds=300)
    session = _session_with_profile(profile, [])

    result = await GraphValidator().validate_definition(
        {"nodes": [{"id": "n1", "node_type": "sandbox_agent", "timeout_seconds": 301}], "edges": []},
        session,
        environment_profile_id=pid,
    )

    assert any(i.code == "SANDBOX_TIMEOUT_EXCEEDS_PROFILE_CAP" for i in result.issues)


async def test_higher_profile_cap_accepts_node_timeout_within_transport_ceiling(monkeypatch):
    pid = uuid.uuid4()
    profile = _mock_profile(pid, max_node_seconds=5000)
    session = _session_with_profile(profile, [])
    monkeypatch.setattr("modulo.core.graph_validator.settings.get_settings", lambda: MagicMock(saq_run_timeout=3300))

    result = await GraphValidator().validate_definition(
        {"nodes": [{"id": "n1", "node_type": "sandbox_agent", "timeout_seconds": 3300}], "edges": []},
        session,
        environment_profile_id=pid,
    )

    assert not any(i.code == "SANDBOX_TIMEOUT_EXCEEDS_PROFILE_CAP" for i in result.issues)


async def test_profile_above_transport_ceiling_is_rejected(monkeypatch):
    pid = uuid.uuid4()
    profile = _mock_profile(pid, max_node_seconds=5000)
    session = _session_with_profile(profile, [])
    monkeypatch.setattr("modulo.core.graph_validator.settings.get_settings", lambda: MagicMock(saq_run_timeout=3300))

    result = await GraphValidator().validate_definition(
        {"nodes": [{"id": "n1", "node_type": "sandbox_agent", "timeout_seconds": 3301}], "edges": []},
        session,
        environment_profile_id=pid,
    )

    assert any(i.code == "ENV_PROFILE_WALL_CLOCK_EXCEEDS_RUN_CEILING" for i in result.issues)
    assert any(i.code == "SANDBOX_TIMEOUT_EXCEEDS_PROFILE_CAP" for i in result.issues)


async def test_empty_required_capabilities_is_valid():
    """If agents have no required capabilities, no error."""
    pid = uuid.uuid4()
    aid = uuid.uuid4()
    profile = _mock_profile(pid, capabilities=["docker"])
    agent = _mock_agent(aid, name="agent-a", required_capabilities=[])

    session = _session_with_profile(profile, [agent])

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

    session = _session_with_profile(profile, [agent])

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

    session = _session_with_profile(profile, [agent])

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

    session = _session_with_profile(profile, [agent1, agent2])

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

    session = _session_with_profile(profile, [agent])

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

    session = _session_with_profile(profile, [agent])

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

    session = _session_with_profile(profile, [])
    # No agents should be fetched

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
