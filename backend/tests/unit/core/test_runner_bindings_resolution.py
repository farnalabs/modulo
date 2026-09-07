"""Unit tests for the real ``resolve_agent_bindings`` provision-time body (FAR-592 / D6).

The integration suite (``tests/integration/crud/test_agent_runner_binding.py``)
exercises this path against a real Postgres, but the integration suite does NOT
feed SonarCloud's new-code coverage (only ``backend-test``'s unit coverage does).
The earlier unit tests for ``runner_bindings`` mock ``resolve_agent_bindings``
out entirely, so the real function body was uncovered by the unit suite. These
tests cover the real body with a mocked session + ModelBackendHub so the
provision-time resolution surface counts toward the SonarCloud gate.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.runner_bindings import (
    AgentBindingResolutionError,
    LocalProviderBindingsRefusedError,
    resolve_agent_bindings,
)


def _binding(model_backend_id: uuid.UUID, target_env_var: str, source_field: str) -> SimpleNamespace:
    return SimpleNamespace(
        model_backend_id=model_backend_id,
        target_env_var=target_env_var,
        source_field=source_field,
    )


def _backend(bid: uuid.UUID, name: str = "openai") -> SimpleNamespace:
    return SimpleNamespace(id=bid, name=name, organisation_id=uuid.UUID(int=0))


def _make_result(rows: list[object] | None = None, scalar: object = None) -> MagicMock:
    result = MagicMock()
    result.scalars.return_value = list(rows or [])
    result.scalar_one_or_none.return_value = scalar
    return result


def _make_session(execute_results: list[MagicMock]) -> tuple[MagicMock, AsyncMock]:
    session = AsyncMock()
    session.execute.side_effect = list(execute_results)
    # ``resolve_agent_bindings`` does ``async with session_factory() as session,
    # session.begin():`` — make both the factory result and ``session.begin()``
    # async context managers that resolve back to the same session object so the
    # body's ``session.execute`` calls land on our side_effect.
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=session)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    session_factory = MagicMock(return_value=session)
    return session_factory, session


@pytest.mark.asyncio
async def test_resolve_empty_when_inputs_missing() -> None:
    assert not await resolve_agent_bindings(session_factory=None, org_id=None, agent_id=None)
    assert not await resolve_agent_bindings(session_factory=MagicMock(), org_id=None, agent_id=uuid.uuid4())
    assert not await resolve_agent_bindings(session_factory=MagicMock(), org_id=uuid.uuid4(), agent_id=None)


@pytest.mark.asyncio
async def test_resolve_empty_when_no_bindings() -> None:
    org_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    session_factory, _ = _make_session([_make_result(rows=[])])
    with (
        patch("modulo.db.rls.set_rls_org"),
        patch("modulo.db.rls.set_rls_execution_context"),
    ):
        resolved = await resolve_agent_bindings(session_factory=session_factory, org_id=org_id, agent_id=agent_id)
    assert resolved == {}


@pytest.mark.asyncio
async def test_resolve_local_provider_refused_without_opt_in() -> None:
    org_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    env_profile_id = uuid.uuid4()
    binding = _binding(uuid.uuid4(), "OPENCODE_API_KEY", "api_key")
    backend = _backend(binding.model_backend_id)
    agent = SimpleNamespace(name="agent-x")
    profile = SimpleNamespace(provider_type="local", config_json={})
    session_factory, _ = _make_session(
        [
            _make_result(rows=[binding]),
            _make_result(rows=[backend]),
            _make_result(scalar=agent),
            _make_result(scalar=profile),
        ]
    )
    with (
        patch("modulo.db.rls.set_rls_org"),
        patch("modulo.db.rls.set_rls_execution_context"),
        patch("modulo.settings.get_settings") as get_settings,
        patch("modulo.core.secrets_backend.create_secrets_backend"),
        patch("modulo.core.model_backend_hub.ModelBackendHub") as model_hub,
    ):
        get_settings.return_value = SimpleNamespace(fernet_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
        hub = AsyncMock()
        hub.creds_for = MagicMock(return_value={"api_key": "secret"})
        model_hub.return_value.__aenter__.return_value = hub
        with pytest.raises(LocalProviderBindingsRefusedError):
            await resolve_agent_bindings(
                session_factory=session_factory,
                org_id=org_id,
                agent_id=agent_id,
                environment_profile_id=env_profile_id,
            )


@pytest.mark.asyncio
async def test_resolve_local_provider_opt_in_allows_bindings() -> None:
    org_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    env_profile_id = uuid.uuid4()
    binding = _binding(uuid.uuid4(), "OPENCODE_API_KEY", "api_key")
    backend = _backend(binding.model_backend_id)
    agent = SimpleNamespace(name="agent-x")
    profile = SimpleNamespace(provider_type="local", config_json={"allow_runner_env_bindings": True})
    session_factory, _ = _make_session(
        [
            _make_result(rows=[binding]),
            _make_result(rows=[backend]),
            _make_result(scalar=agent),
            _make_result(scalar=profile),
        ]
    )
    with (
        patch("modulo.db.rls.set_rls_org"),
        patch("modulo.db.rls.set_rls_execution_context"),
        patch("modulo.settings.get_settings") as get_settings,
        patch("modulo.core.secrets_backend.create_secrets_backend"),
        patch("modulo.core.model_backend_hub.ModelBackendHub") as model_hub,
    ):
        get_settings.return_value = SimpleNamespace(fernet_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
        hub = AsyncMock()
        hub.creds_for = MagicMock(return_value={"api_key": "secret"})
        model_hub.return_value.__aenter__.return_value = hub
        resolved = await resolve_agent_bindings(
            session_factory=session_factory,
            org_id=org_id,
            agent_id=agent_id,
            environment_profile_id=env_profile_id,
        )
    assert resolved == {"OPENCODE_API_KEY": "secret"}


@pytest.mark.asyncio
async def test_resolve_source_field_missing_raises() -> None:
    org_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    binding = _binding(uuid.uuid4(), "OPENCODE_API_KEY", "api_key")
    backend = _backend(binding.model_backend_id)
    agent = SimpleNamespace(name="agent-x")
    session_factory, _ = _make_session(
        [
            _make_result(rows=[binding]),
            _make_result(rows=[backend]),
            _make_result(scalar=agent),
        ]
    )
    with (
        patch("modulo.db.rls.set_rls_org"),
        patch("modulo.db.rls.set_rls_execution_context"),
        patch("modulo.settings.get_settings") as get_settings,
        patch("modulo.core.secrets_backend.create_secrets_backend"),
        patch("modulo.core.model_backend_hub.ModelBackendHub") as model_hub,
    ):
        get_settings.return_value = SimpleNamespace(fernet_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
        hub = AsyncMock()
        hub.creds_for = MagicMock(return_value={})  # no creds at all
        model_hub.return_value.__aenter__.return_value = hub
        with pytest.raises(AgentBindingResolutionError):
            await resolve_agent_bindings(session_factory=session_factory, org_id=org_id, agent_id=agent_id)


@pytest.mark.asyncio
async def test_resolve_happy_path_injects_credentials() -> None:
    org_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    binding = _binding(uuid.uuid4(), "OPENCODE_API_KEY", "api_key")
    backend = _backend(binding.model_backend_id)
    agent = SimpleNamespace(name="agent-x")
    session_factory, _ = _make_session(
        [
            _make_result(rows=[binding]),
            _make_result(rows=[backend]),
            _make_result(scalar=agent),
        ]
    )
    with (
        patch("modulo.db.rls.set_rls_org"),
        patch("modulo.db.rls.set_rls_execution_context"),
        patch("modulo.settings.get_settings") as get_settings,
        patch("modulo.core.secrets_backend.create_secrets_backend"),
        patch("modulo.core.model_backend_hub.ModelBackendHub") as model_hub,
    ):
        get_settings.return_value = SimpleNamespace(fernet_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
        hub = AsyncMock()
        hub.creds_for = MagicMock(return_value={"api_key": "topsecret"})
        model_hub.return_value.__aenter__.return_value = hub
        resolved = await resolve_agent_bindings(
            session_factory=session_factory,
            org_id=org_id,
            agent_id=agent_id,
            run_id="run-1",
            node_id="node-1",
        )
    assert resolved == {"OPENCODE_API_KEY": "topsecret"}
