"""FAR-1128 cap-environments: dispatch a real profile through the real hub.

Exercises the full resolve → dispatch lifecycle with NO stubs: a profile with
``provider_type="local"`` is resolved by the real ``RuntimeProviderHub`` from
``build_hub()`` to the real ``LocalRuntimeProvider``, which creates an actual
workspace on the host, executes a real subprocess (``sys.executable``) with
asserted stdout, and tears the workspace down. The two failure modes use the
hub's own deterministic resolution contract: a known-but-unregistered type
raises ``ProviderNotConfiguredError`` and a type outside the
``PROVIDER_TYPES`` vocabulary raises ``UnknownProviderTypeError``.
"""

from __future__ import annotations

import sys
import uuid
from types import SimpleNamespace

import pytest

from modulo.core.runtime_provider import (
    ProviderNotConfiguredError,
    UnknownProviderTypeError,
    WorkspaceSpec,
    build_hub,
)
from modulo.core.runtime_provider.local import LocalRuntimeProvider


def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MODULO_E2B_API_KEY", raising=False)
    monkeypatch.delenv("MODULO_DOCKER_HOST", raising=False)
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.delenv("MODULO_RUNNER_DOCKER_HOST", raising=False)


def _local_profile() -> SimpleNamespace:
    return SimpleNamespace(provider_type="local", provider_hint=None)


class TestCapEnvironmentsDispatch:
    async def test_local_profile_dispatch_creates_execs_and_destroys(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clean_env(monkeypatch)
        hub = build_hub()

        provider = hub.resolve(_local_profile())
        assert isinstance(provider, LocalRuntimeProvider)

        spec = WorkspaceSpec(
            environment_profile_id=uuid.uuid4(),
            organisation_id=uuid.uuid4(),
            image_ref="local",
            capabilities=["shell"],
            timeout_seconds=30,
        )
        workspace = await provider.create_workspace(spec)
        try:
            result = await provider.exec_command(
                workspace,
                [sys.executable, "-c", "print('cap-environments-dispatched')"],
            )
        finally:
            await provider.destroy_workspace(workspace)

        assert result.exit_code == 0
        assert "cap-environments-dispatched" in result.stdout
        assert await provider.get_workspace_status(workspace) == "terminated"
        assert workspace not in provider._workspaces

    def test_known_unregistered_provider_type_raises_provider_not_configured(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _clean_env(monkeypatch)
        hub = build_hub()

        profile = SimpleNamespace(provider_type="e2b", provider_hint=None)
        with pytest.raises(ProviderNotConfiguredError):
            hub.resolve(profile)

    def test_unknown_provider_type_raises_unknown_provider_type(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clean_env(monkeypatch)
        hub = build_hub()

        profile = SimpleNamespace(provider_type="quantum-bubble", provider_hint=None)
        with pytest.raises(UnknownProviderTypeError):
            hub.resolve(profile)
