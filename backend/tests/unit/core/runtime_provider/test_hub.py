"""Unit tests for RuntimeProviderHub."""

import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.runtime_provider import ExecResult, RuntimeProvider, WorkspaceSpec
from modulo.core.runtime_provider.docker import DockerRuntimeProvider
from modulo.core.runtime_provider.e2b import E2BRuntimeProvider
from modulo.core.runtime_provider.hub import RuntimeProviderHub
from modulo.core.runtime_provider.local import LocalRuntimeProvider


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


@pytest.mark.parametrize(
    ("provider_type", "expected"),
    [
        ("local", "local"),
        ("local_docker", "docker"),
        ("docker", "docker"),
        ("e2b", "e2b"),
    ],
)
def test_resolve_honours_explicit_provider_type(provider_type: str, expected: str) -> None:
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

    assert hub.resolve(SimpleNamespace(provider_type=provider_type)) is providers[expected]


def test_resolve_explicit_unavailable_provider_does_not_fall_back() -> None:
    hub = RuntimeProviderHub()
    hub.register("local", LocalRuntimeProvider())

    assert hub.resolve(SimpleNamespace(provider_type="e2b")) is None


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


class _SpyProvider(_StubProvider):
    """Stub provider that records create/destroy calls for lease lifecycle tests."""

    def __init__(self, name: str = "spy") -> None:
        super().__init__(name)
        self.destroyed: list[Any] = []
        self.created: list[WorkspaceSpec] = []

    async def destroy_workspace(self, provider_ref: str) -> None:
        self.destroyed.append(provider_ref)

    async def create_workspace(self, spec: WorkspaceSpec) -> str:
        self.created.append(spec)
        return f"{self.name}-{spec.environment_profile_id}"


def _lease_session(existing_lease: Any = None) -> AsyncMock:
    """Return an async session whose SELECT returns *existing_lease* or None."""
    session = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = existing_lease
    session.execute = AsyncMock(return_value=result)
    session.add = MagicMock()
    return session


def _lease_profile(**overrides: Any) -> Any:
    """Return a minimal EnvironmentProfile-like object."""
    defaults: dict[str, Any] = {
        "id": uuid.uuid4(),
        "organisation_id": uuid.uuid4(),
        "provider_type": None,
        "provider_hint": None,
        "image_ref": "python:3.12",
        "capabilities_json": ["shell"],
        "config_json": {},
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


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
# create_lease — workspace lease lifecycle
# ---------------------------------------------------------------------------


class TestCreateLease:
    async def test_returns_existing_lease(self) -> None:
        hub = RuntimeProviderHub()
        provider = _SpyProvider("docker")
        hub.register("docker", provider)
        existing = MagicMock()
        session = _lease_session(existing)
        run_id = uuid.uuid4()

        lease = await hub.create_lease(_lease_profile(), run_id, session)

        assert lease is existing
        assert not provider.created
        session.add.assert_not_called()

    async def test_creates_lease_from_profile(self) -> None:
        hub = RuntimeProviderHub()
        provider = _SpyProvider("docker")
        hub.register("docker", provider)
        profile = _lease_profile()
        run_id = uuid.uuid4()
        session = _lease_session(None)

        lease = await hub.create_lease(profile, run_id, session)

        assert lease.run_id == run_id
        assert lease.organisation_id == profile.organisation_id
        assert lease.environment_profile_id == profile.id
        assert lease.status == "running"
        assert lease.provider_ref == f"docker-{profile.id}"
        assert lease.lease_started_at is not None
        assert lease.lease_expires_at is not None
        session.add.assert_called_once_with(lease)

    async def test_builds_workspace_spec_from_profile(self) -> None:
        hub = RuntimeProviderHub()
        provider = _SpyProvider("docker")
        hub.register("docker", provider)
        run_id = uuid.uuid4()
        profile = _lease_profile(
            image_ref="custom:1.0",
            capabilities_json=["shell", "network"],
            config_json={"repo_url": "https://github.com/acme/app", "repo_ref": "main", "memory_mb": 1024},
        )

        await hub.create_lease(profile, run_id, _lease_session(None))

        assert len(provider.created) == 1
        spec = provider.created[0]
        assert spec.environment_profile_id == profile.id
        assert spec.organisation_id == profile.organisation_id
        assert spec.run_id == run_id
        assert spec.image_ref == "custom:1.0"
        assert spec.capabilities == ["shell", "network"]
        assert spec.labels == {"repo_url": "https://github.com/acme/app", "repo_ref": "main"}
        assert spec.resource_limits == {"memory_mb": 1024}

    async def test_dict_ref_uses_ref_key(self) -> None:
        class _DictProvider(_SpyProvider):
            async def create_workspace(self, spec: WorkspaceSpec) -> Any:
                return {"ref": "ws-abc123"}

        hub = RuntimeProviderHub()
        hub.register("docker", _DictProvider("docker"))

        lease = await hub.create_lease(_lease_profile(), uuid.uuid4(), _lease_session(None))

        assert lease.provider_ref == "ws-abc123"

    async def test_dict_ref_falls_back_to_container_id(self) -> None:
        class _ContainerIdProvider(_SpyProvider):
            async def create_workspace(self, spec: WorkspaceSpec) -> Any:
                return {"container_id": "c-12345"}

        hub = RuntimeProviderHub()
        hub.register("docker", _ContainerIdProvider("docker"))

        lease = await hub.create_lease(_lease_profile(), uuid.uuid4(), _lease_session(None))

        assert lease.provider_ref == "c-12345"

    async def test_raises_without_registered_provider(self) -> None:
        hub = RuntimeProviderHub()
        with pytest.raises(ValueError, match="No RuntimeProvider registered"):
            await hub.create_lease(_lease_profile(), uuid.uuid4(), _lease_session(None))


# ---------------------------------------------------------------------------
# destroy_lease — workspace teardown
# ---------------------------------------------------------------------------


class TestDestroyLease:
    async def test_destroys_workspace_and_marks_completed(self) -> None:
        hub = RuntimeProviderHub()
        provider = _SpyProvider("docker")
        hub.register("docker", provider)
        lease = SimpleNamespace(environment_profile=None, provider_ref="ref-1", status="running")

        await hub.destroy_lease(lease)

        assert provider.destroyed == ["ref-1"]
        assert lease.status == "completed"

    async def test_resolves_provider_from_profile(self) -> None:
        hub = RuntimeProviderHub()
        provider = _SpyProvider("docker")
        hub.register("docker", provider)
        profile = SimpleNamespace(provider_type="docker", provider_hint=None)
        lease = SimpleNamespace(environment_profile=profile, provider_ref="ref-abc", status="running")

        await hub.destroy_lease(lease)

        assert provider.destroyed == ["ref-abc"]
        assert lease.status == "completed"

    async def test_uses_provider_ref_when_present(self) -> None:
        hub = RuntimeProviderHub()
        provider = _SpyProvider("docker")
        hub.register("docker", provider)
        lease = SimpleNamespace(environment_profile=None, provider_ref="", status="running")

        await hub.destroy_lease(lease)

        # empty provider_ref falls back to the lease object itself
        assert provider.destroyed == [lease]
        assert lease.status == "completed"

    async def test_no_provider_logs_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        hub = RuntimeProviderHub()
        lease = SimpleNamespace(environment_profile=None, provider_ref="ref-1", status="running")

        await hub.destroy_lease(lease)

        assert "No RuntimeProvider registered" in caplog.text
        assert lease.status == "running"

    async def test_destroy_failure_logs_but_does_not_raise(self, caplog: pytest.LogCaptureFixture) -> None:
        class _FailingProvider(_StubProvider):
            async def destroy_workspace(self, provider_ref: str) -> None:
                raise RuntimeError("boom")

        hub = RuntimeProviderHub()
        hub.register("docker", _FailingProvider("docker"))
        lease = SimpleNamespace(environment_profile=None, provider_ref="ref-1", status="running")

        await hub.destroy_lease(lease)

        assert "Failed to destroy workspace" in caplog.text
        assert lease.status == "completed"

    async def test_adds_lease_to_session(self) -> None:
        hub = RuntimeProviderHub()
        provider = _SpyProvider("docker")
        hub.register("docker", provider)
        session = MagicMock()
        lease = SimpleNamespace(environment_profile=None, provider_ref="ref-1", status="running")

        await hub.destroy_lease(lease, session)

        session.add.assert_called_once_with(lease)
