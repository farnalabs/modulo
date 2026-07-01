"""Unit tests for LocalRuntimeProvider."""

import asyncio
import sys
import uuid
from pathlib import Path

import pytest

from modulo.core.runtime_provider import WorkspaceSpec
from modulo.core.runtime_provider.local import LocalRuntimeProvider, create_local_provider_from_env


@pytest.fixture
def provider() -> LocalRuntimeProvider:
    return LocalRuntimeProvider(max_concurrency=4)


@pytest.fixture
def spec() -> WorkspaceSpec:
    return WorkspaceSpec(
        environment_profile_id=uuid.uuid4(),
        organisation_id=uuid.uuid4(),
        image_ref="local",
        capabilities=["shell"],
        timeout_seconds=30,
        labels={"profile_name": "test"},
    )


class TestLocalRuntimeProvider:
    async def test_create_and_destroy_workspace(self, provider: LocalRuntimeProvider, spec: WorkspaceSpec) -> None:
        ref = await provider.create_workspace(spec)
        assert ref in provider._workspaces
        assert Path(provider._workspaces[ref]).is_dir()

        await provider.destroy_workspace(ref)
        assert ref not in provider._workspaces

    async def test_exec_echo(self, provider: LocalRuntimeProvider, spec: WorkspaceSpec) -> None:
        ref = await provider.create_workspace(spec)
        result = await provider.exec_command(ref, [sys.executable, "-c", "print('hello world')"])
        assert "hello world" in result.stdout
        assert result.exit_code == 0
        await provider.destroy_workspace(ref)

    async def test_exec_failure(self, provider: LocalRuntimeProvider, spec: WorkspaceSpec) -> None:
        ref = await provider.create_workspace(spec)
        result = await provider.exec_command(ref, [sys.executable, "-c", "import sys; sys.exit(42)"])
        assert result.exit_code == 42
        await provider.destroy_workspace(ref)

    async def test_exec_unknown_workspace(self, provider: LocalRuntimeProvider) -> None:
        with pytest.raises(ValueError, match="Unknown workspace"):
            await provider.exec_command("nonexistent", ["echo", "hi"])

    async def test_destroy_unknown_workspace(self, provider: LocalRuntimeProvider) -> None:
        await provider.destroy_workspace("nonexistent")

    async def test_get_workspace_status(self, provider: LocalRuntimeProvider, spec: WorkspaceSpec) -> None:
        ref = await provider.create_workspace(spec)
        assert await provider.get_workspace_status(ref) == "running"
        await provider.destroy_workspace(ref)
        assert await provider.get_workspace_status(ref) == "terminated"

    async def test_concurrency_semaphore_blocks(self, provider: LocalRuntimeProvider, spec: WorkspaceSpec) -> None:
        tight_provider = LocalRuntimeProvider(max_concurrency=1)
        ref = await tight_provider.create_workspace(spec)

        started_event = asyncio.Event()
        can_finish_event = asyncio.Event()

        async def slow_command() -> None:
            started_event.set()
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-c", "import time; time.sleep(20)",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            can_finish_event.set()
            await proc.wait()

        task = asyncio.create_task(slow_command())

        await started_event.wait()

        asyncio.get_running_loop().time()
        second_task = asyncio.create_task(
            tight_provider.exec_command(ref, [sys.executable, "-c", "print('second')"])
        )

        await asyncio.sleep(0.05)
        assert not second_task.done(), "Second command should be blocked by semaphore"

        can_finish_event.set()

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        await tight_provider.destroy_workspace(ref)

    async def test_supports_local_hint(self, provider: LocalRuntimeProvider) -> None:
        class FakeProfile:
            provider_hint = "local"

        assert provider.supports(FakeProfile()) is True

    async def test_supports_e2b_hint(self, provider: LocalRuntimeProvider) -> None:
        class FakeProfile:
            provider_hint = "e2b"
            image_ref = ""

        assert provider.supports(FakeProfile()) is False

    async def test_supports_no_hint(self, provider: LocalRuntimeProvider) -> None:
        class FakeProfile:
            provider_hint = ""
            image_ref = ""

        assert provider.supports(FakeProfile()) is True

    async def test_create_local_provider_from_env_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MODULO_MAX_LOCAL_CONCURRENCY", raising=False)
        p = create_local_provider_from_env()
        assert p._max_concurrency == 2

    async def test_create_local_provider_from_env_custom(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MODULO_MAX_LOCAL_CONCURRENCY", "10")
        p = create_local_provider_from_env()
        assert p._max_concurrency == 10

    async def test_create_local_provider_from_env_invalid(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MODULO_MAX_LOCAL_CONCURRENCY", "not-a-number")
        p = create_local_provider_from_env()
        assert p._max_concurrency == 2

    async def test_timeout(self, provider: LocalRuntimeProvider, spec: WorkspaceSpec) -> None:
        ref = await provider.create_workspace(spec)
        result = await provider.exec_command(
            ref,
            [sys.executable, "-c", "import time; time.sleep(10)"],
            timeout=1,
        )
        assert result.exit_code == -1
        assert "timed out" in result.stderr
        await provider.destroy_workspace(ref)
