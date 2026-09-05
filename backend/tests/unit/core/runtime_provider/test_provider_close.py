"""Unit tests for provider/close teardown paths (hub-aclose disposal, ADR 029).

These exercise the per-provider ``close()`` best-effort teardown and the hub's
``aclose()`` bounded-disposal branches that the deadline refactor added but the
happy-path tests never reach.
"""

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from modulo.core.runtime_provider import RuntimeProvider, WorkspaceSpec, build_hub
from modulo.core.runtime_provider.docker import DockerRuntimeProvider
from modulo.core.runtime_provider.e2b import E2BRuntimeProvider
from modulo.core.runtime_provider.hub import RuntimeProviderHub
from modulo.core.runtime_provider.local import LocalRuntimeProvider


def _spec(**kwargs: Any) -> WorkspaceSpec:
    import uuid

    return WorkspaceSpec(
        environment_profile_id=uuid.uuid4(),
        organisation_id=uuid.uuid4(),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# LocalRuntimeProvider.close()
# ---------------------------------------------------------------------------


async def test_local_close_destroy_best_effort(monkeypatch: pytest.MonkeyPatch) -> None:
    """close() removes every tracked workspace, swallowing per-workspace errors."""
    provider = LocalRuntimeProvider()
    provider._workspaces["ws1"] = "/tmp/ws1"

    destroyed: list[str] = []

    async def _destroy(ref: str) -> None:
        destroyed.append(ref)

    monkeypatch.setattr(provider, "destroy_workspace", _destroy)
    await provider.close()

    assert destroyed == ["ws1"]


async def test_local_close_logs_destroy_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A destroy failure is logged and never masks the remaining teardown."""
    provider = LocalRuntimeProvider()
    provider._workspaces["ws1"] = "/tmp/ws1"

    async def _destroy(ref: str) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(provider, "destroy_workspace", _destroy)
    # Must not raise.
    await provider.close()

    # ---------------------------------------------------------------------------
    # DockerRuntimeProvider.close()
    # ---------------------------------------------------------------------------

    async def test_docker_close_destroy_timeout_force_drops(monkeypatch: pytest.MonkeyPatch) -> None:
        """A hung destroy is force-dropped (timeout branch) and teardown continues."""
        provider = DockerRuntimeProvider()
        provider._workspaces["ws1"] = "ws1"

        async def _timeout(ref: str) -> None:
            raise TimeoutError("hung daemon")

        monkeypatch.setattr(provider, "destroy_workspace", _timeout)
        provider._client = AsyncMock()
        provider._client.close = AsyncMock()

        # Must not raise; the loop continues to the client close.
        await provider.close()

        assert provider._client.close.await_count == 1


async def test_docker_close_destroy_exception_logged(monkeypatch: pytest.MonkeyPatch) -> None:
    """A generic destroy failure is logged and does not mask the client close."""
    provider = DockerRuntimeProvider()
    provider._workspaces["ws1"] = "ws1"

    async def _boom(ref: str) -> None:
        raise RuntimeError("oops")

    monkeypatch.setattr(provider, "destroy_workspace", _boom)
    client = AsyncMock()
    client.close = AsyncMock()
    provider._client = client

    await provider.close()

    assert client.close.await_count == 1


async def test_docker_close_client_timeout_force_drops(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hung client close is force-dropped (timeout branch)."""
    provider = DockerRuntimeProvider()
    provider._workspaces.clear()
    provider._client = AsyncMock()
    provider._client.close = AsyncMock(side_effect=TimeoutError("hung"))

    await provider.close()

    assert provider._client is None


async def test_docker_close_client_exception_logged(monkeypatch: pytest.MonkeyPatch) -> None:
    """A generic client close failure is logged and clears the client ref."""
    provider = DockerRuntimeProvider()
    provider._workspaces.clear()
    provider._client = AsyncMock()
    provider._client.close = AsyncMock(side_effect=RuntimeError("oops"))

    await provider.close()

    assert provider._client is None


# ---------------------------------------------------------------------------
# E2BRuntimeProvider.create_workspace(workspace_metadata) + close()
# ---------------------------------------------------------------------------


async def test_e2b_create_workspace_passes_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-empty workspace_metadata maps to the E2B sandbox ``metadata`` kwarg."""
    provider = E2BRuntimeProvider(api_key="test-key")

    sandbox = SimpleNamespace(
        sandbox_id="sb1",
        commands=SimpleNamespace(run=AsyncMock()),
        is_running=AsyncMock(return_value=True),
    )

    calls: list[dict[str, Any]] = []

    class _FakeSandbox:
        @staticmethod
        async def create(**kwargs: Any) -> SimpleNamespace:
            calls.append(kwargs)
            return sandbox

    import e2b as _e2b_mod

    monkeypatch.setattr(_e2b_mod, "AsyncSandbox", _FakeSandbox)

    spec = _spec(workspace_metadata={"org": "acme", "run": "42"})
    ref = await provider.create_workspace(spec)

    assert ref == "sb1"
    assert calls[0]["metadata"] == {"org": "acme", "run": "42"}


async def test_e2b_close_destroy_best_effort(monkeypatch: pytest.MonkeyPatch) -> None:
    """close() destroys every tracked sandbox, swallowing per-sandbox errors."""
    provider = E2BRuntimeProvider(api_key="test-key")
    provider._sandboxes["sb1"] = SimpleNamespace()

    destroyed: list[str] = []

    async def _destroy(ref: str) -> None:
        destroyed.append(ref)

    monkeypatch.setattr(provider, "destroy_workspace", _destroy)
    await provider.close()

    assert destroyed == ["sb1"]


async def test_e2b_close_logs_destroy_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A destroy failure is logged and never masks the remaining teardown."""
    provider = E2BRuntimeProvider(api_key="test-key")
    provider._sandboxes["sb1"] = SimpleNamespace()

    async def _destroy(ref: str) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(provider, "destroy_workspace", _destroy)
    await provider.close()


# ---------------------------------------------------------------------------
# RuntimeProviderHub.aclose()
# ---------------------------------------------------------------------------


class _NeverCloseProvider(RuntimeProvider):
    provider_id = "stuck"

    async def create_workspace(self, spec: WorkspaceSpec) -> str:
        return "ws"

    async def exec_command(self, provider_ref: str, command: list[str], *, cmd_timeout: int | None = None) -> Any:
        raise NotImplementedError

    async def destroy_workspace(self, provider_ref: str) -> None:
        pass

    async def get_workspace_status(self, provider_ref: str) -> str:
        return "running"

    async def close(self) -> None:
        raise TimeoutError("hung provider")


class _BoomProvider(RuntimeProvider):
    provider_id = "boom"

    async def create_workspace(self, spec: WorkspaceSpec) -> str:
        return "ws"

    async def exec_command(self, provider_ref: str, command: list[str], *, cmd_timeout: int | None = None) -> Any:
        raise NotImplementedError

    async def destroy_workspace(self, provider_ref: str) -> None:
        pass

    async def get_workspace_status(self, provider_ref: str) -> str:
        return "running"

    async def close(self) -> None:
        raise RuntimeError("oops")


async def test_hub_aclose_force_drops_timed_out_provider() -> None:
    """A provider whose close() times out is force-dropped from the registry."""
    hub = RuntimeProviderHub()
    stuck = _NeverCloseProvider()
    hub.register("stuck", stuck)

    await hub.aclose()

    assert hub.get("stuck") is None


async def test_hub_aclose_logs_provider_exception() -> None:
    """A provider whose close() raises is logged and stays registered (no force-drop)."""
    hub = RuntimeProviderHub()
    boom = _BoomProvider()
    hub.register("boom", boom)

    await hub.aclose()

    assert hub.get("boom") is boom


async def test_hub_aclose_closes_all_registered(monkeypatch: pytest.MonkeyPatch) -> None:
    """aclose() disposes every registered provider, including the always-on local."""
    hub = build_hub()
    local = hub.get("local")
    assert local is not None

    closed = []

    async def _close() -> None:
        closed.append(True)

    monkeypatch.setattr(local, "close", _close)
    await hub.aclose()

    assert closed == [True]


# ---------------------------------------------------------------------------
# DockerRuntimeProvider.create_workspace(workspace_metadata) -> Labels
# ---------------------------------------------------------------------------


async def test_docker_create_workspace_maps_workspace_metadata_to_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-empty workspace_metadata maps to container Labels (ADR 029)."""
    provider = DockerRuntimeProvider()
    client = AsyncMock()
    container = SimpleNamespace(id="c1", start=AsyncMock())
    client.containers.create = AsyncMock(return_value=container)
    monkeypatch.setattr(provider, "_get_client", AsyncMock(return_value=client))

    spec = _spec(workspace_metadata={"org": "acme", "run": "42"})
    ref = await provider.create_workspace(spec)

    assert len(ref) == 12
    created = client.containers.create.call_args.kwargs
    assert created["config"]["Labels"] == {"org": "acme", "run": "42"}
