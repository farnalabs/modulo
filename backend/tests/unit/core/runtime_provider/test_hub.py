"""Unit tests for RuntimeProviderHub."""

from typing import Any
from unittest.mock import MagicMock

import pytest

from modulo.core.runtime_provider import ExecResult, RuntimeProvider, WorkspaceSpec
from modulo.core.runtime_provider.hub import RuntimeProviderHub


class _StubProvider(RuntimeProvider):
    """Minimal concrete provider for testing."""

    def __init__(self, name: str = "stub") -> None:
        self.name = name
        self._supports_check: Any = lambda p: False

    def set_supports(self, fn: Any) -> None:
        self._supports_check = fn

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

    def supports(self, profile: Any) -> bool:
        return self._supports_check(profile)  # type: ignore[no-any-return]


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
    hub.unregister("nope")  # should not raise


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
    assert hub.list_providers() != {}


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


def test_resolve_hint_preferred_over_supports() -> None:
    """provider_hint takes priority even if another provider supports the profile."""
    hub = RuntimeProviderHub()
    docker = _StubProvider("docker")
    k8s = _StubProvider("k8s")
    k8s.set_supports(lambda p: True)
    hub.register("docker", docker)
    hub.register("k8s", k8s)

    profile = MagicMock()
    profile.provider_hint = "docker"
    assert hub.resolve(profile) is docker


def test_resolve_by_supports() -> None:
    """When no hint, resolve by calling supports() on each provider."""
    hub = RuntimeProviderHub()
    docker = _StubProvider("docker")
    k8s = _StubProvider("k8s")
    k8s.set_supports(lambda p: True)  # k8s supports it
    hub.register("docker", docker)
    hub.register("k8s", k8s)

    profile = MagicMock()
    profile.provider_hint = None
    assert hub.resolve(profile) is k8s


def test_resolve_first_provider_fallback() -> None:
    """When no hint and no supports returns True, fall back to first registered."""
    hub = RuntimeProviderHub()
    docker = _StubProvider("docker")
    k8s = _StubProvider("k8s")
    hub.register("docker", docker)
    hub.register("k8s", k8s)

    profile = MagicMock()
    profile.provider_hint = None
    assert hub.resolve(profile) is docker


def test_resolve_empty_hub_returns_none() -> None:
    hub = RuntimeProviderHub()
    profile = MagicMock()
    profile.provider_hint = None
    assert hub.resolve(profile) is None


def test_resolve_hint_not_found_continues() -> None:
    """If hint doesn't match, fall through to supports/first."""
    hub = RuntimeProviderHub()
    k8s = _StubProvider("k8s")
    k8s.set_supports(lambda p: True)
    hub.register("k8s", k8s)

    profile = MagicMock()
    profile.provider_hint = "nonexistent"
    assert hub.resolve(profile) is k8s


def test_supports_exception_does_not_block() -> None:
    """If a provider's supports() raises, skip it and continue."""
    hub = RuntimeProviderHub()

    docker = _StubProvider("docker")
    docker.set_supports(lambda p: (_ for _ in ()).throw(RuntimeError("boom")))

    k8s = _StubProvider("k8s")
    k8s.set_supports(lambda p: True)

    hub.register("docker", docker)
    hub.register("k8s", k8s)

    profile = MagicMock()
    profile.provider_hint = None
    assert hub.resolve(profile) is k8s


def test_provider_without_supports_skipped() -> None:
    """If a provider lacks a supports attribute, skip it during supports-based resolution."""
    hub = RuntimeProviderHub()
    docker = _StubProvider("docker")
    # Remove the supports method
    docker.supports = None  # type: ignore[assignment]
    k8s = _StubProvider("k8s")
    k8s.set_supports(lambda p: True)
    hub.register("docker", docker)
    hub.register("k8s", k8s)

    profile = MagicMock()
    profile.provider_hint = None
    assert hub.resolve(profile) is k8s


def test_resolve_profile_without_provider_hint_attr() -> None:
    """A profile that doesn't have a provider_hint attribute at all."""
    hub = RuntimeProviderHub()
    k8s = _StubProvider("k8s")
    k8s.set_supports(lambda p: True)
    hub.register("k8s", k8s)

    profile = MagicMock(spec=[])  # object without provider_hint
    assert hub.resolve(profile) is k8s


def test_resolve_profile_without_provider_hint_attr_fallback_to_first() -> None:
    """No hint and no supports match -> fallback to first registered."""
    hub = RuntimeProviderHub()
    docker = _StubProvider("docker")
    k8s = _StubProvider("k8s")
    hub.register("docker", docker)
    hub.register("k8s", k8s)

    profile = MagicMock(spec=[])
    assert hub.resolve(profile) is docker


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


def test_resolve_with_hint_after_unregister() -> None:
    """If a hint-matching provider was unregistered, fall through."""
    hub = RuntimeProviderHub()
    docker = _StubProvider("docker")
    k8s = _StubProvider("k8s")
    k8s.set_supports(lambda p: True)
    hub.register("docker", docker)
    hub.register("k8s", k8s)
    hub.unregister("docker")

    profile = MagicMock()
    profile.provider_hint = "docker"
    assert hub.resolve(profile) is k8s
