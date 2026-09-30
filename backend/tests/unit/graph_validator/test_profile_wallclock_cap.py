"""FAR-1359 — a sandbox node's wall-clock limit comes from the SELECTED profile.

Before FAR-1359 ``GraphValidator`` rejected any sandbox node's
``timeout_seconds`` above a hardcoded 3300 — E2B's 1-hour platform cap plus
provisioning headroom — which assumed E2B's limit was universal. The cap is now
the bound environment profile's ``max_node_seconds`` (the PROVIDER's capability),
so a provider that can host long-running agents raises its own profiles while an
E2B customer still sees the same 1-hour limit, and a capability above the deploy's
run transport ceiling is rejected loudly instead of producing a run that the SAQ
transport would later kill silently.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock

from modulo.core.graph_validator import GraphValidator, ValidationResult
from modulo.db.models.environment_profile import DEFAULT_MAX_NODE_SECONDS


def _sandbox_node(timeout_seconds: int) -> dict:
    return {
        "id": str(uuid.uuid4()),
        "node_type": "sandbox_agent",
        "agent_prompt": "Do the thing",
        "agent_commands": ["opencode run"],
        "template_id": "opencode",
        "timeout_seconds": timeout_seconds,
    }


def _mock_profile(max_node_seconds: int) -> MagicMock:
    profile = MagicMock()
    profile.id = uuid.uuid4()
    profile.name = "e2b-default"
    profile.capabilities_json = []
    profile.max_node_seconds = max_node_seconds
    return profile


def _session(profile: MagicMock | None) -> AsyncMock:
    session = AsyncMock()
    session.get = AsyncMock(return_value=profile)
    scalars_result = MagicMock()
    scalars_result.all.return_value = []
    execute_result = MagicMock()
    execute_result.scalars.return_value = scalars_result
    session.execute = AsyncMock(return_value=execute_result)
    return session


def _codes(result: ValidationResult) -> set[str]:
    return {issue.code for issue in result.issues}


# ---------------------------------------------------------------------------
# No profile bound — the shipped default (unchanged behaviour)
# ---------------------------------------------------------------------------


async def test_no_profile_bound_uses_the_shipped_default_cap() -> None:
    """No profile bound -> the pre-FAR-1359 3300 default still rejects 3600."""
    result = await GraphValidator().validate_definition(
        {"nodes": [_sandbox_node(3600)], "edges": []},
        _session(None),
        environment_profile_id=None,
    )
    assert not result.is_valid
    assert "SANDBOX_TIMEOUT_EXCEEDS_PROFILE_CAP" in _codes(result)


async def test_unknown_profile_id_falls_back_to_the_shipped_default_cap() -> None:
    """A dangling profile id resolves to the default cap; ENV_PROFILE_NOT_FOUND
    is owned by ``_check_environment_capabilities`` (it must not double-report)."""
    result = await GraphValidator().validate_definition(
        {"nodes": [_sandbox_node(3600)], "edges": []},
        _session(None),
        environment_profile_id=uuid.uuid4(),
    )
    assert "SANDBOX_TIMEOUT_EXCEEDS_PROFILE_CAP" in _codes(result)
    assert "ENV_PROFILE_MAX_NODE_SECONDS_EXCEEDS_RUN_CEILING" not in _codes(result)


# ---------------------------------------------------------------------------
# A bound profile's capability is the cap
# ---------------------------------------------------------------------------


async def test_profile_capability_admits_a_long_running_agent(monkeypatch) -> None:
    """A provider that can host long-running agents declares a larger cap (and
    the deployment raises the run ceiling to match), and a node above the old
    hardcoded 3300 then saves cleanly."""
    monkeypatch.setenv("MODULO_MAX_RUN_SECONDS", "86400")
    result = await GraphValidator().validate_definition(
        {"nodes": [_sandbox_node(21600)], "edges": []},
        _session(_mock_profile(86400)),
        environment_profile_id=uuid.uuid4(),
    )
    assert result.is_valid, [i.message for i in result.issues]
    assert "SANDBOX_TIMEOUT_EXCEEDS_PROFILE_CAP" not in _codes(result)


async def test_profile_capability_rejects_a_node_above_it() -> None:
    """The same node is rejected when the SELECTED profile's capability is lower
    than the node's timeout — the cap moved with the profile, not with the code."""
    result = await GraphValidator().validate_definition(
        {"nodes": [_sandbox_node(21600)], "edges": []},
        _session(_mock_profile(DEFAULT_MAX_NODE_SECONDS)),
        environment_profile_id=uuid.uuid4(),
    )
    assert not result.is_valid
    issue = next(i for i in result.issues if i.code == "SANDBOX_TIMEOUT_EXCEEDS_PROFILE_CAP")
    assert "21600" in issue.message
    assert str(DEFAULT_MAX_NODE_SECONDS) in issue.message
    assert "3300" in issue.message


async def test_node_exactly_at_the_profile_capability_saves() -> None:
    """Boundary: timeout_seconds == the profile's capability is accepted."""
    result = await GraphValidator().validate_definition(
        {"nodes": [_sandbox_node(3600)], "edges": []},
        _session(_mock_profile(3600)),
        environment_profile_id=uuid.uuid4(),
    )
    assert result.is_valid, [i.message for i in result.issues]


# ---------------------------------------------------------------------------
# Capability above the run transport ceiling — loud and early
# ---------------------------------------------------------------------------


async def test_capability_above_the_run_ceiling_is_rejected(monkeypatch) -> None:
    """A profile asking for more wall-clock than a whole run can get is rejected
    at save time, naming the ceiling — never admitted to be SAQ-killed later."""
    monkeypatch.setenv("MODULO_MAX_RUN_SECONDS", "7200")
    result = await GraphValidator().validate_definition(
        {"nodes": [_sandbox_node(7200)], "edges": []},
        _session(_mock_profile(86400)),
        environment_profile_id=uuid.uuid4(),
    )
    assert not result.is_valid
    issue = next(i for i in result.issues if i.code == "ENV_PROFILE_MAX_NODE_SECONDS_EXCEEDS_RUN_CEILING")
    assert "86400" in issue.message
    assert "7200" in issue.message
    assert "MODULO_MAX_RUN_SECONDS" in issue.message


async def test_capability_at_the_run_ceiling_is_accepted(monkeypatch) -> None:
    """A capability exactly AT the ceiling is legal — the ceiling is a bound, not
    an off-by-one rejection."""
    monkeypatch.setenv("MODULO_MAX_RUN_SECONDS", "86400")
    result = await GraphValidator().validate_definition(
        {"nodes": [_sandbox_node(3600)], "edges": []},
        _session(_mock_profile(86400)),
        environment_profile_id=uuid.uuid4(),
    )
    assert result.is_valid, [i.message for i in result.issues]
    assert "ENV_PROFILE_MAX_NODE_SECONDS_EXCEEDS_RUN_CEILING" not in _codes(result)


# ---------------------------------------------------------------------------
# The static per-node check takes the resolved cap
# ---------------------------------------------------------------------------


def test_static_sandbox_check_honours_an_explicit_cap() -> None:
    """Callers that already resolved a profile cap pass it straight through."""
    result = ValidationResult()
    GraphValidator._check_sandbox_agent_config({"nodes": [_sandbox_node(3600)], "edges": []}, result, 7200)
    assert "SANDBOX_TIMEOUT_EXCEEDS_PROFILE_CAP" not in _codes(result)
    assert result.is_valid


def test_static_sandbox_check_defaults_to_the_shipped_cap() -> None:
    """Omitting the cap keeps the shipped 3300 default (no silent relaxation)."""
    result = ValidationResult()
    GraphValidator._check_sandbox_agent_config({"nodes": [_sandbox_node(3600)], "edges": []}, result)
    assert "SANDBOX_TIMEOUT_EXCEEDS_PROFILE_CAP" in _codes(result)
