"""FAR-592 (D6): unit tests for ``modulo.core.runner_bindings.resolve_agent_bindings``.

The sandbox integration suite patches ``resolve_agent_bindings`` wholesale (it
only asserts the NODE merges the returned dict), so the real function body was
otherwise unexercised. These tests drive the real resolver with a mocked
session factory + ``ModelBackendHub`` to cover the provision-time resolution
branches (happy path, missing backend, unavailable source field, Local-tier
refusal, opt-in, and the early-return guards).
"""

from __future__ import annotations

import uuid
from typing import Any, Self
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.runner_bindings import (
    AgentBindingResolutionError,
    LocalProviderBindingsRefusedError,
    resolve_agent_bindings,
)

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_AGENT_ID = uuid.UUID("00000000-0000-0000-0000-0000000000aa")
_BACKEND_ID = uuid.UUID("00000000-0000-0000-0000-0000000000bb")
_ENV_PROFILE_ID = uuid.UUID("00000000-0000-0000-0000-0000000000cc")


class _Hub:
    """Async-context-manager stand-in for ``ModelBackendHub``."""

    def __init__(self, creds: dict[str, str] | None = None) -> None:
        self.creds = creds if creds is not None else {"api_key": "secret-value"}
        self.initialise = AsyncMock()
        self.creds_for = MagicMock(return_value=self.creds)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False


def _backend_row(name: str = "openai-backend", provider: str = "openai") -> MagicMock:
    row = MagicMock()
    row.id = _BACKEND_ID
    row.name = name
    row.provider = provider
    return row


def _binding_row(target_env_var: str = "OPENCODE_API_KEY", source_field: str = "api_key") -> MagicMock:
    row = MagicMock()
    row.agent_id = _AGENT_ID
    row.model_backend_id = _BACKEND_ID
    row.target_env_var = target_env_var
    row.source_field = source_field
    return row


def _agent_row(name: str = "my-agent") -> MagicMock:
    row = MagicMock()
    row.name = name
    return row


def _make_session_factory(
    *,
    bindings: list[MagicMock],
    backends: list[MagicMock],
    agent: MagicMock | None = None,
    profile: MagicMock | None = None,
) -> MagicMock:
    """Build a session_factory context manager whose execute() returns rows in call order."""
    results: list[MagicMock] = []

    def _scalars(rows: list[MagicMock]) -> MagicMock:
        r = MagicMock()
        r.scalars.return_value = list(rows)
        r.scalar_one_or_none.return_value = rows[0] if rows else None
        return r

    results.append(_scalars(bindings))  # 1. agent bindings
    results.append(_scalars(backends))  # 2. model backends
    results.append(_scalars([agent] if agent is not None else []))  # 3. agent
    if profile is not None:
        results.append(_scalars([profile]))  # 4. env profile (optional)

    execute = AsyncMock(side_effect=results)

    session = MagicMock()
    session.execute = execute

    class _SessionCM:
        async def __aenter__(self) -> MagicMock:
            return session

        async def __aexit__(self, *_exc: object) -> bool:
            return False

    # The outer ``async with session_factory() as session, session.begin():``
    # needs both the factory call and session.begin() to be async CMs.
    begin_cm = MagicMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)

    return MagicMock(return_value=_SessionCM())


def _settings_patch() -> MagicMock:
    settings = MagicMock()
    settings.fernet_key = "a" * 32
    return settings


def _patch_resolver(hub: _Hub, settings: MagicMock = _settings_patch()) -> list[Any]:
    patchers = [
        patch("modulo.core.model_backend_hub.ModelBackendHub", return_value=hub),
        patch("modulo.core.secrets_backend.create_secrets_backend", return_value=MagicMock()),
        patch("modulo.db.rls.set_rls_org", new=AsyncMock()),
        patch("modulo.db.rls.set_rls_execution_context", new=AsyncMock()),
        patch("modulo.settings.get_settings", return_value=settings),
    ]
    for p in patchers:
        p.start()
    return patchers


async def test_resolve_returns_empty_when_session_factory_none() -> None:
    assert not await resolve_agent_bindings(session_factory=None, org_id=_ORG_ID, agent_id=_AGENT_ID)


async def test_resolve_returns_empty_when_org_id_invalid() -> None:
    factory = _make_session_factory(bindings=[], backends=[])
    patchers = _patch_resolver(_Hub())
    try:
        assert not await resolve_agent_bindings(session_factory=factory, org_id="not-a-uuid", agent_id=_AGENT_ID)
    finally:
        for p in patchers:
            p.stop()


async def test_resolve_returns_empty_when_no_bindings() -> None:
    factory = _make_session_factory(bindings=[], backends=[])
    patchers = _patch_resolver(_Hub())
    try:
        assert not await resolve_agent_bindings(session_factory=factory, org_id=_ORG_ID, agent_id=_AGENT_ID)
    finally:
        for p in patchers:
            p.stop()


async def test_resolve_happy_path_injects_env_var() -> None:
    factory = _make_session_factory(
        bindings=[_binding_row()],
        backends=[_backend_row()],
        agent=_agent_row("agent-x"),
    )
    patchers = _patch_resolver(_Hub(creds={"api_key": "topsecret"}))
    try:
        resolved = await resolve_agent_bindings(
            session_factory=factory,
            org_id=_ORG_ID,
            agent_id=_AGENT_ID,
            run_id="run-1",
            node_id="node-1",
        )
    finally:
        for p in patchers:
            p.stop()
    assert resolved == {"OPENCODE_API_KEY": "topsecret"}


async def test_resolve_missing_backend_raises_resolution_error() -> None:
    # Backend referenced by the binding is absent from the org's backend set.
    other_backend = uuid.uuid4()
    binding = _binding_row()
    binding.model_backend_id = other_backend
    factory = _make_session_factory(bindings=[binding], backends=[_backend_row()], agent=_agent_row())
    patchers = _patch_resolver(_Hub())
    try:
        with pytest.raises(AgentBindingResolutionError):
            await resolve_agent_bindings(session_factory=factory, org_id=_ORG_ID, agent_id=_AGENT_ID)
    finally:
        for p in patchers:
            p.stop()


async def test_resolve_unavailable_source_field_raises() -> None:
    binding = _binding_row(source_field="api_key")
    factory = _make_session_factory(bindings=[binding], backends=[_backend_row()], agent=_agent_row())
    # Hub returns creds WITHOUT the requested source field.
    patchers = _patch_resolver(_Hub(creds={"other_field": "x"}))
    try:
        with pytest.raises(AgentBindingResolutionError):
            await resolve_agent_bindings(session_factory=factory, org_id=_ORG_ID, agent_id=_AGENT_ID)
    finally:
        for p in patchers:
            p.stop()


async def test_resolve_local_tier_without_opt_in_refused() -> None:
    profile = MagicMock()
    profile.provider_type = "local"
    profile.config_json = {}
    factory = _make_session_factory(
        bindings=[_binding_row()],
        backends=[_backend_row()],
        agent=_agent_row("agent-y"),
        profile=profile,
    )
    patchers = _patch_resolver(_Hub())
    try:
        with pytest.raises(LocalProviderBindingsRefusedError):
            await resolve_agent_bindings(
                session_factory=factory,
                org_id=_ORG_ID,
                agent_id=_AGENT_ID,
                environment_profile_id=_ENV_PROFILE_ID,
            )
    finally:
        for p in patchers:
            p.stop()


async def test_resolve_local_tier_with_opt_in_proceeds() -> None:
    profile = MagicMock()
    profile.provider_type = "local"
    profile.config_json = {"allow_runner_env_bindings": True}
    factory = _make_session_factory(
        bindings=[_binding_row()],
        backends=[_backend_row()],
        agent=_agent_row("agent-z"),
        profile=profile,
    )
    patchers = _patch_resolver(_Hub(creds={"api_key": "ok"}))
    try:
        resolved = await resolve_agent_bindings(
            session_factory=factory,
            org_id=_ORG_ID,
            agent_id=_AGENT_ID,
            environment_profile_id=_ENV_PROFILE_ID,
        )
    finally:
        for p in patchers:
            p.stop()
    assert resolved == {"OPENCODE_API_KEY": "ok"}


async def test_resolve_non_local_profile_does_not_require_opt_in() -> None:
    profile = MagicMock()
    profile.provider_type = "runner_docker"
    profile.config_json = {}
    factory = _make_session_factory(
        bindings=[_binding_row()],
        backends=[_backend_row()],
        agent=_agent_row("agent-w"),
        profile=profile,
    )
    patchers = _patch_resolver(_Hub(creds={"api_key": "ok"}))
    try:
        resolved = await resolve_agent_bindings(
            session_factory=factory,
            org_id=_ORG_ID,
            agent_id=_AGENT_ID,
            environment_profile_id=_ENV_PROFILE_ID,
        )
    finally:
        for p in patchers:
            p.stop()
    assert resolved == {"OPENCODE_API_KEY": "ok"}


async def test_resolve_unknown_profile_provider_passes() -> None:
    # provider_type that is not "local" -> no refusal regardless of config.
    profile = MagicMock()
    profile.provider_type = "  E2B  "
    profile.config_json = {}
    factory = _make_session_factory(
        bindings=[_binding_row()],
        backends=[_backend_row()],
        agent=_agent_row("agent-q"),
        profile=profile,
    )
    patchers = _patch_resolver(_Hub(creds={"api_key": "ok"}))
    try:
        resolved = await resolve_agent_bindings(
            session_factory=factory,
            org_id=_ORG_ID,
            agent_id=_AGENT_ID,
            environment_profile_id=_ENV_PROFILE_ID,
        )
    finally:
        for p in patchers:
            p.stop()
    assert resolved == {"OPENCODE_API_KEY": "ok"}
