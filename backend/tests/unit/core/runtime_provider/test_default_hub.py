"""Unit tests for build_hub (factory) and RuntimeProvider ABC metadata matching."""

from typing import Any, cast
from unittest.mock import patch

import pytest

from modulo.core.runtime_provider import (
    ExecResult,
    RuntimeProvider,
    WorkspaceSpec,
    build_hub,
    create_default_hub,
)
from modulo.core.runtime_provider.docker import DockerRuntimeProvider
from modulo.core.runtime_provider.e2b import E2BRuntimeProvider
from modulo.core.runtime_provider.local import LocalRuntimeProvider


class _MinimalProvider(RuntimeProvider):
    provider_id = "local"

    async def create_workspace(self, spec: WorkspaceSpec) -> str:
        return "ws"

    async def exec_command(
        self, provider_ref: str, command: list[str], *, cmd_timeout: int | None = None
    ) -> ExecResult:
        raise NotImplementedError

    async def destroy_workspace(self, provider_ref: str) -> None:
        pass

    async def get_workspace_status(self, provider_ref: str) -> str:
        return "running"


class TestBuildHub:
    def test_registers_local_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MODULO_E2B_API_KEY", raising=False)
        monkeypatch.delenv("MODULO_DOCKER_HOST", raising=False)
        monkeypatch.delenv("DOCKER_HOST", raising=False)
        monkeypatch.delenv("MODULO_RUNNER_DOCKER_HOST", raising=False)

        hub = build_hub()

        assert isinstance(hub.get("local"), LocalRuntimeProvider)
        assert hub.get("e2b") is None
        assert hub.get("runner_docker") is None

    def test_default_local_concurrency_is_two(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MODULO_E2B_API_KEY", raising=False)
        monkeypatch.delenv("MODULO_DOCKER_HOST", raising=False)
        monkeypatch.delenv("DOCKER_HOST", raising=False)
        monkeypatch.delenv("MODULO_RUNNER_DOCKER_HOST", raising=False)

        hub = build_hub()

        assert cast(LocalRuntimeProvider, hub.get("local"))._max_concurrency == 2

    def test_clamps_invalid_concurrency(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MODULO_E2B_API_KEY", raising=False)
        monkeypatch.delenv("MODULO_DOCKER_HOST", raising=False)
        monkeypatch.delenv("DOCKER_HOST", raising=False)
        monkeypatch.delenv("MODULO_RUNNER_DOCKER_HOST", raising=False)

        hub = create_default_hub(max_local_concurrency=0)

        assert cast(LocalRuntimeProvider, hub.get("local"))._max_concurrency == 2

    def test_registers_e2b_when_api_key_env_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MODULO_E2B_API_KEY", "test-key")
        monkeypatch.delenv("MODULO_DOCKER_HOST", raising=False)
        monkeypatch.delenv("DOCKER_HOST", raising=False)

        hub = build_hub()

        provider = hub.get("e2b")
        assert isinstance(provider, E2BRuntimeProvider)
        assert provider._api_key == "test-key"

    def test_registers_runner_docker_when_host_env_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MODULO_E2B_API_KEY", raising=False)
        monkeypatch.delenv("MODULO_DOCKER_HOST", raising=False)
        monkeypatch.setenv("DOCKER_HOST", "tcp://localhost:2375")

        hub = build_hub()

        assert isinstance(hub.get("runner_docker"), DockerRuntimeProvider)

    def test_registers_runner_docker_when_modulo_docker_host_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MODULO_E2B_API_KEY", raising=False)
        monkeypatch.delenv("DOCKER_HOST", raising=False)
        monkeypatch.setenv("MODULO_DOCKER_HOST", "tcp://localhost:2375")

        hub = build_hub()

        assert isinstance(hub.get("runner_docker"), DockerRuntimeProvider)

    def test_registers_runner_docker_on_any_modulo_runner_signal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MODULO_E2B_API_KEY", raising=False)
        monkeypatch.delenv("MODULO_DOCKER_HOST", raising=False)
        monkeypatch.delenv("DOCKER_HOST", raising=False)
        monkeypatch.setenv("MODULO_RUNNER_TEMPLATE_ID", "opencode")

        hub = build_hub()

        assert isinstance(hub.get("runner_docker"), DockerRuntimeProvider)

    def test_no_runner_docker_without_any_signal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MODULO_E2B_API_KEY", raising=False)
        monkeypatch.delenv("MODULO_DOCKER_HOST", raising=False)
        monkeypatch.delenv("DOCKER_HOST", raising=False)
        monkeypatch.delenv("MODULO_RUNNER_DOCKER_HOST", raising=False)
        monkeypatch.delenv("MODULO_RUNNER_TEMPLATE_ID", raising=False)

        hub = build_hub()

        assert hub.get("runner_docker") is None

    def test_skips_e2b_on_import_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MODULO_E2B_API_KEY", "test-key")
        monkeypatch.delenv("MODULO_DOCKER_HOST", raising=False)
        monkeypatch.delenv("DOCKER_HOST", raising=False)

        real_import = cast(Any, __import__)

        def _fake_import(name: str, *args: object, **kwargs: object) -> object:
            if name == "modulo.core.runtime_provider.e2b":
                raise ImportError("e2b not installed")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=_fake_import):
            hub = build_hub()

        assert hub.get("e2b") is None
        assert isinstance(hub.get("local"), LocalRuntimeProvider)

    def test_skips_docker_on_import_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MODULO_E2B_API_KEY", raising=False)
        monkeypatch.setenv("DOCKER_HOST", "tcp://localhost:2375")

        real_import = cast(Any, __import__)

        def _fake_import(name: str, *args: object, **kwargs: object) -> object:
            if name == "modulo.core.runtime_provider.docker":
                raise ImportError("docker not installed")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=_fake_import):
            hub = build_hub()

        assert hub.get("runner_docker") is None
        assert isinstance(hub.get("local"), LocalRuntimeProvider)


class TestMatchesProviderType:
    def test_matches_provider_id(self) -> None:
        provider = _MinimalProvider()
        assert provider.matches_provider_type("local") is True

    def test_matches_provider_alias(self) -> None:
        provider = _MinimalProvider()
        provider.provider_aliases = frozenset({"docker", "local-docker"})
        assert provider.matches_provider_type("docker") is True
        assert provider.matches_provider_type("local-docker") is True

    def test_matches_are_case_insensitive(self) -> None:
        provider = _MinimalProvider()
        assert provider.matches_provider_type("LOCAL") is True
        assert provider.matches_provider_type("  Local  ") is True

    def test_no_match_returns_false(self) -> None:
        provider = _MinimalProvider()
        assert provider.matches_provider_type("e2b") is False

    def test_blank_type_returns_false(self) -> None:
        provider = _MinimalProvider()
        assert provider.matches_provider_type("") is False
        assert provider.matches_provider_type("   ") is False

    async def test_close_is_noop(self) -> None:
        provider = _MinimalProvider()
        result = await provider.close()
        assert result is None


# ---------------------------------------------------------------------------
# Factory-alias
# ---------------------------------------------------------------------------


def test_create_default_hub_is_build_hub_alias() -> None:
    """The pre-rename factory name keeps working (public-API stability)."""
    assert create_default_hub is build_hub


# ---------------------------------------------------------------------------
# Env-signal parity (single source of truth, FAR-587)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("provider_type", "env_var"),
    [
        ("e2b", "MODULO_E2B_API_KEY"),
        ("runner_docker", "MODULO_DOCKER_HOST"),
        ("docker", "MODULO_DOCKER_HOST"),
        ("local_docker", "MODULO_DOCKER_HOST"),
    ],
)
def test_documented_env_var_actually_registers_provider(
    provider_type: str,
    env_var: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Parity pin: setting each documented env var registers the provider.

    The documented signal (env_var_for_provider_type -> remediation copy) and
    the implemented build_hub gate must agree: a profile whose provider_type
    maps to ``env_var`` MUST resolve once only that var is set.
    """
    for var in ("MODULO_E2B_API_KEY", "MODULO_DOCKER_HOST", "DOCKER_HOST"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("MODULO_RUNNER_TEMPLATE_ID", raising=False)
    monkeypatch.setenv(env_var, "registration-signal")

    hub = build_hub()
    provider = hub.get(
        "e2b" if provider_type == "e2b" else "runner_docker",
    )
    assert provider is not None

    resolved = hub.resolve(type("P", (), {"provider_type": provider_type})())
    assert resolved.provider_id == provider.provider_id


def test_retry_failure_signal_alone_registers_runner_docker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Parity pin: a bare MODULO_RUNNER_* signal (non-Docker-host) registers the provider."""
    monkeypatch.delenv("MODULO_E2B_API_KEY", raising=False)
    monkeypatch.delenv("MODULO_DOCKER_HOST", raising=False)
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.setenv("MODULO_RUNNER_RETRY_FAILURE_SCALE", "2.0")

    hub = build_hub()
    assert isinstance(hub.get("runner_docker"), DockerRuntimeProvider)
