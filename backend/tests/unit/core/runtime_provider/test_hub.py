"""Unit tests for RuntimeProviderHub."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from modulo.core.runtime_provider import (
    ExecResult,
    ProviderNotConfiguredError,
    RuntimeProvider,
    WorkspaceSpec,
)
from modulo.core.runtime_provider.docker import DockerRuntimeProvider
from modulo.core.runtime_provider.e2b import E2BRuntimeProvider
from modulo.core.runtime_provider.hub import RuntimeProviderHub
from modulo.core.runtime_provider.local import LocalRuntimeProvider


class _StubProvider(RuntimeProvider):
    """Minimal concrete provider for testing."""

    def __init__(self, name: str = "stub") -> None:
        self.name = name

    async def create_workspace(self, spec: WorkspaceSpec) -> str:
        return f"{self.name}-{spec.environment_profile_id}"

    async def exec_command(
        self,
        provider_ref: str,
        command: list[str],
        *,
        timeout: int | None = None,  # noqa: ASYNC109
    ) -> ExecResult:
        from modulo.core.runtime_provider import ExecResult

        return ExecResult(exit_code=0, stdout="", stderr="")

    async def destroy_workspace(self, provider_ref: str) -> None:
        pass

    async def get_workspace_status(self, provider_ref: str) -> str:
        return "running"


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_register_and_get() -> None:
    hub = RuntimeProviderHub()
    provider = _StubProvider("docker")
    hub.register("docker", provider)
    assert hub.get("docker") is provider


def test_register_duplicate_raises() -> None:
    hub = RuntimeProviderHub()
    hub.register("docker", _StubProvider("docker"))
    with pytest.raises(ValueError, match="already registered"):
        hub.register("docker", _StubProvider("docker2"))


def test_get_nonexistent_returns_none() -> None:
    hub = RuntimeProviderHub()
    assert hub.get("missing") is None


def test_unregister() -> None:
    hub = RuntimeProviderHub()
    hub.register("docker", _StubProvider("docker"))
    hub.unregister("docker")
    assert hub.get("docker") is None


def test_unregister_nonexistent_does_not_raise() -> None:
    hub = RuntimeProviderHub()
    hub.register("docker", _StubProvider("docker"))
    hub.unregister("nope")  # should not raise
    assert hub.get("docker") is not None
    assert hub.get("nope") is None


def test_list_providers_returns_copy() -> None:
    hub = RuntimeProviderHub()
    a = _StubProvider("a")
    b = _StubProvider("b")
    hub.register("a", a)
    hub.register("b", b)
    providers = hub.list_providers()
    assert set(providers.keys()) == {"a", "b"}
    assert providers["a"] is a
    # Mutating returned dict should not affect hub
    providers.clear()
    assert hub.list_providers() == {"a": a, "b": b}


# ---------------------------------------------------------------------------
# Resolve
# ---------------------------------------------------------------------------


def test_resolve_by_hint() -> None:
    """If the profile has a provider_hint matching a registered name, use it."""
    hub = RuntimeProviderHub()
    docker = _StubProvider("docker")
    k8s = _StubProvider("k8s")
    hub.register("docker", docker)
    hub.register("k8s", k8s)

    profile = MagicMock()
    profile.provider_hint = "docker"
    assert hub.resolve(profile) is docker


def test_resolve_hint_preferred_over_type() -> None:
    """provider_hint takes priority even if provider_type names another provider."""
    hub = RuntimeProviderHub()
    docker = _StubProvider("docker")
    k8s = _StubProvider("k8s")
    hub.register("docker", docker)
    hub.register("k8s", k8s)

    profile = SimpleNamespace(provider_hint="docker", provider_type="k8s")
    assert hub.resolve(profile) is docker


def test_resolve_by_explicit_provider_type_order_independent() -> None:
    """Profile provider types resolve independently of registration order."""
    hub = RuntimeProviderHub()
    providers = {
        "e2b": E2BRuntimeProvider(api_key="test-key"),
        "local": LocalRuntimeProvider(),
        "docker": DockerRuntimeProvider(),
    }
    hub.register("cloud-sandbox", providers["e2b"])
    hub.register("host-process", providers["local"])
    hub.register("container-runtime", providers["docker"])

    for provider_type in ("local_docker", "docker", "runner_docker"):
        assert hub.resolve(SimpleNamespace(provider_type=provider_type)) is providers["docker"]
    assert hub.resolve(SimpleNamespace(provider_type="local")) is providers["local"]
    assert hub.resolve(SimpleNamespace(provider_type="e2b")) is providers["e2b"]


def test_resolve_unregistered_provider_type_raises_with_env_var() -> None:
    """An explicit type whose provider is not registered raises the typed error."""
    hub = RuntimeProviderHub()
    hub.register("local", LocalRuntimeProvider())

    with pytest.raises(ProviderNotConfiguredError, match="MODULO_E2B_API_KEY") as exc_info:
        hub.resolve(SimpleNamespace(provider_type="e2b"))
    assert exc_info.value.provider_type == "e2b"
    assert exc_info.value.env_var == "MODULO_E2B_API_KEY"


def test_resolve_docker_type_without_docker_env_raises_with_remediation() -> None:
    """legacy local_docker profiles surface the Docker env-var remediation."""
    hub = RuntimeProviderHub()
    hub.register("local", LocalRuntimeProvider())

    for legacy_type in ("local_docker", "docker", "runner_docker"):
        with pytest.raises(ProviderNotConfiguredError, match="MODULO_DOCKER_HOST") as exc_info:
            hub.resolve(SimpleNamespace(provider_type=legacy_type))
        assert exc_info.value.env_var == "MODULO_DOCKER_HOST"


def test_resolve_unknown_provider_type_raises_without_fallback() -> None:
    """Unknown provider types raise; no first-registered fallback exists."""
    hub = RuntimeProviderHub()
    hub.register("local", LocalRuntimeProvider())

    with pytest.raises(ProviderNotConfiguredError, match="k8s") as exc_info:
        hub.resolve(SimpleNamespace(provider_type="k8s"))
    assert exc_info.value.env_var is None


def test_resolve_missing_provider_type_raises() -> None:
    """A profile without any provider_type is unresolvable — no guessing."""
    hub = RuntimeProviderHub()
    hub.register("local", LocalRuntimeProvider())

    with pytest.raises(ProviderNotConfiguredError):
        hub.resolve(SimpleNamespace(provider_hint=None))


def test_resolve_empty_hub_raises() -> None:
    hub = RuntimeProviderHub()

    with pytest.raises(ProviderNotConfiguredError):
        hub.resolve(SimpleNamespace(provider_type="local", provider_hint=None))


def test_resolve_hint_not_found_falls_through_to_type() -> None:
    """A stale hint falls through to the explicit provider_type match."""
    hub = RuntimeProviderHub()
    k8s = _StubProvider("k8s")
    hub.register("k8s", k8s)

    profile = SimpleNamespace(provider_hint="nonexistent", provider_type="k8s")
    assert hub.resolve(profile) is k8s


def test_resolve_hint_not_found_and_unresolvable_type_raises() -> None:
    """A stale hint plus an unregistered provider_type raises the typed error."""
    hub = RuntimeProviderHub()
    k8s = _StubProvider("k8s")
    hub.register("k8s", k8s)

    profile = SimpleNamespace(provider_hint="nonexistent", provider_type="e2b")
    with pytest.raises(ProviderNotConfiguredError):
        hub.resolve(profile)


def test_register_and_list_after_unregister() -> None:
    """After unregister, the provider no longer appears in list_providers."""
    hub = RuntimeProviderHub()
    a = _StubProvider("a")
    b = _StubProvider("b")
    hub.register("a", a)
    hub.register("b", b)
    hub.unregister("a")
    providers = hub.list_providers()
    assert "a" not in providers
    assert "b" in providers


# ---------------------------------------------------------------------------
# initialise — factory-load providers from config
# ---------------------------------------------------------------------------


class TestInitialise:
    async def test_registers_local_docker(self) -> None:
        hub = RuntimeProviderHub()
        await hub.initialise(
            {"container-runtime": {"type": "local_docker", "docker_host": "unix:///var/run/docker.sock"}}
        )
        provider = hub.get("container-runtime")
        assert isinstance(provider, DockerRuntimeProvider)
        assert provider._docker_host == "unix:///var/run/docker.sock"

    async def test_registers_local_docker_with_default_image(self) -> None:
        hub = RuntimeProviderHub()
        await hub.initialise({"container-runtime": {"type": "local_docker"}})
        provider = hub.get("container-runtime")
        assert isinstance(provider, DockerRuntimeProvider)
        assert provider._default_image == "python:3.12-slim"

    async def test_infers_type_from_provider_name(self) -> None:
        hub = RuntimeProviderHub()
        await hub.initialise({"local_docker": {}})
        assert isinstance(hub.get("local_docker"), DockerRuntimeProvider)

    async def test_registers_runner_docker_type(self) -> None:
        hub = RuntimeProviderHub()
        await hub.initialise({"container-runtime": {"type": "runner_docker"}})
        provider = hub.get("container-runtime")
        assert isinstance(provider, DockerRuntimeProvider)

    async def test_registers_e2b_with_api_key(self) -> None:
        hub = RuntimeProviderHub()
        await hub.initialise({"sandbox": {"type": "e2b", "api_key": "test-key"}})
        provider = hub.get("sandbox")
        assert isinstance(provider, E2BRuntimeProvider)
        assert provider._api_key == "test-key"

    async def test_skips_e2b_without_api_key(self, caplog: pytest.LogCaptureFixture) -> None:
        hub = RuntimeProviderHub()
        await hub.initialise({"sandbox": {"type": "e2b"}})
        assert hub.get("sandbox") is None
        assert "has no api_key" in caplog.text

    async def test_skips_unknown_provider_type(self, caplog: pytest.LogCaptureFixture) -> None:
        hub = RuntimeProviderHub()
        await hub.initialise({"mystery": {"type": "not-a-provider"}})
        assert hub.get("mystery") is None
        assert "Unknown provider type" in caplog.text

    async def test_skips_already_registered_provider(self, caplog: pytest.LogCaptureFixture) -> None:
        hub = RuntimeProviderHub()
        hub.register("sandbox", _StubProvider("sandbox"))
        await hub.initialise({"sandbox": {"type": "e2b", "api_key": "test-key"}})
        assert "already registered, skipping factory init" in caplog.text
        assert isinstance(hub.get("sandbox"), _StubProvider)

    async def test_empty_config_is_noop(self) -> None:
        hub = RuntimeProviderHub()
        await hub.initialise({})
        assert not hub.list_providers()

    async def test_local_docker_register_race_logs_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        """If a concurrent task registers the name first, log instead of crashing."""
        hub = RuntimeProviderHub()

        def _raise_duplicate(name: str, provider: Any) -> None:
            raise ValueError(f"RuntimeProvider '{name}' is already registered")

        with patch.object(hub, "register", side_effect=_raise_duplicate):
            await hub.initialise({"container-runtime": {"type": "local_docker"}})

        assert "already registered, skipping" in caplog.text
        assert hub.get("container-runtime") is None

    async def test_e2b_register_race_logs_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        hub = RuntimeProviderHub()

        def _raise_duplicate(name: str, provider: Any) -> None:
            raise ValueError(f"RuntimeProvider '{name}' is already registered")

        with patch.object(hub, "register", side_effect=_raise_duplicate):
            await hub.initialise({"sandbox": {"type": "e2b", "api_key": "test-key"}})

        assert "already registered, skipping" in caplog.text
        assert hub.get("sandbox") is None


# ---------------------------------------------------------------------------
# aclose — per-provision disposal
# ---------------------------------------------------------------------------


class TestAclose:
    async def test_aclose_closes_every_provider(self) -> None:
        hub = RuntimeProviderHub()
        provider_a = _StubProvider("a")
        provider_b = _StubProvider("b")
        hub.register("a", provider_a)
        hub.register("b", provider_b)

        closed: list[str] = []

        async def _track_close() -> None:
            closed.append("yes")

        with (
            patch.object(provider_a, "close", side_effect=_track_close),
            patch.object(provider_b, "close", side_effect=_track_close),
        ):
            await hub.aclose()

        assert closed == ["yes", "yes"]

    async def test_aclose_failure_does_not_block_remaining_providers(self, caplog: pytest.LogCaptureFixture) -> None:
        hub = RuntimeProviderHub()
        failing = _StubProvider("failing")
        healthy = _StubProvider("healthy")
        hub.register("failing", failing)
        hub.register("healthy", healthy)

        async def _boom() -> None:
            raise RuntimeError("boom")

        closed: list[bool] = []

        async def _track_close() -> None:
            closed.append(True)

        with (
            patch.object(failing, "close", side_effect=_boom),
            patch.object(healthy, "close", side_effect=_track_close),
        ):
            await hub.aclose()

        assert closed == [True]
        assert "Failed to close runtime provider" in caplog.text
