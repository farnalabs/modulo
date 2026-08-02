"""Unit tests for LocalRuntimeProvider."""

import asyncio
import sys
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from anyio import Path

from modulo.core.runtime_provider import ExecResult, WorkspaceSpec
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
        assert await Path(provider._workspaces[ref]).is_dir()

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
        assert await provider.get_workspace_status("nonexistent") == "terminated"

    async def test_get_workspace_status(self, provider: LocalRuntimeProvider, spec: WorkspaceSpec) -> None:
        ref = await provider.create_workspace(spec)
        assert await provider.get_workspace_status(ref) == "running"
        await provider.destroy_workspace(ref)
        assert await provider.get_workspace_status(ref) == "terminated"

    async def test_concurrency_semaphore_blocks(
        self,
        monkeypatch: pytest.MonkeyPatch,
        spec: WorkspaceSpec,
    ) -> None:
        tight_provider = LocalRuntimeProvider(max_concurrency=1)
        ref = await tight_provider.create_workspace(spec)

        started_event = asyncio.Event()
        can_finish_event = asyncio.Event()
        process_count = 0

        class BlockingProcess:
            returncode: int | None = None

            async def communicate(self) -> tuple[bytes, bytes]:
                started_event.set()
                await can_finish_event.wait()
                self.returncode = 0
                return b"done", b""

        async def create_process(*args: object, **kwargs: object) -> BlockingProcess:
            nonlocal process_count
            process_count += 1
            return BlockingProcess()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)

        first_task = asyncio.create_task(tight_provider.exec_command(ref, ["first"]))
        await started_event.wait()
        second_task = asyncio.create_task(tight_provider.exec_command(ref, [sys.executable, "-c", "print('second')"]))

        await asyncio.sleep(0.05)
        assert not second_task.done(), "Second command should be blocked by semaphore"
        assert process_count == 1

        can_finish_event.set()
        first_result, second_result = await asyncio.gather(first_task, second_task)
        assert first_result.exit_code == 0
        assert second_result.exit_code == 0
        assert process_count == 2

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
            cmd_timeout=1,
        )
        assert result.exit_code == -1
        assert "timed out" in result.stderr
        await provider.destroy_workspace(ref)

    async def test_exec_spawn_failure_reports_error(
        self, provider: LocalRuntimeProvider, spec: WorkspaceSpec, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _no_such_binary(*args: object, **kwargs: object) -> object:
            raise FileNotFoundError("no such binary")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _no_such_binary)
        ref = await provider.create_workspace(spec)

        result = await provider.exec_command(ref, ["definitely-not-a-binary"])

        assert result.exit_code == -1
        assert "Failed to start process" in result.stderr
        await provider.destroy_workspace(ref)

    async def test_exec_timeout_kills_process(
        self, provider: LocalRuntimeProvider, spec: WorkspaceSpec, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proc = MagicMock()
        proc.communicate = AsyncMock()
        proc.kill = MagicMock()
        proc.wait = AsyncMock()
        monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=proc))

        async def _wait_for(coro: object, timeout: int) -> object:  # noqa: ASYNC109
            await coro  # type: ignore[misc]
            raise TimeoutError

        monkeypatch.setattr(asyncio, "wait_for", _wait_for)
        ref = await provider.create_workspace(spec)

        result = await provider.exec_command(ref, ["sleep", "100"], cmd_timeout=1)

        assert result.exit_code == -1
        assert result.stderr == "Command timed out"
        proc.kill.assert_called_once()
        await provider.destroy_workspace(ref)

    async def test_exec_generic_failure_kills_and_reports(
        self, provider: LocalRuntimeProvider, spec: WorkspaceSpec, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proc = MagicMock()
        proc.communicate = AsyncMock()
        proc.kill = MagicMock()
        proc.wait = AsyncMock()
        monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=proc))

        wait_calls = {"n": 0}

        async def _wait_for(coro: object, timeout: int) -> object:  # noqa: ASYNC109
            wait_calls["n"] += 1
            await coro  # type: ignore[misc]
            if wait_calls["n"] == 1:
                raise OSError("communicate broke")
            raise TimeoutError

        monkeypatch.setattr(asyncio, "wait_for", _wait_for)
        ref = await provider.create_workspace(spec)

        result = await provider.exec_command(ref, ["sleep", "100"])

        assert result.exit_code == -1
        assert result.stderr == "Command execution failed"
        proc.kill.assert_called_once()
        await provider.destroy_workspace(ref)

    async def test_exec_cancellation_propagates(
        self, provider: LocalRuntimeProvider, spec: WorkspaceSpec, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _wait_for(coro: object, timeout: int) -> object:  # noqa: ASYNC109
            await coro  # type: ignore[misc]
            raise asyncio.CancelledError

        monkeypatch.setattr(asyncio, "wait_for", _wait_for)
        ref = await provider.create_workspace(spec)

        with pytest.raises(asyncio.CancelledError):
            await provider.exec_command(ref, ["sleep", "1"])
        await provider.destroy_workspace(ref)

    async def test_create_workspace_clones_repo(
        self, provider: LocalRuntimeProvider, spec: WorkspaceSpec, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        spec.labels = {"repo_url": "https://github.com/acme/app"}
        clone_calls: list[tuple[list[str], str]] = []

        async def _fake_run(command: list[str], cwd: str, cmd_timeout: int | None) -> ExecResult:
            clone_calls.append((command, cwd))
            return ExecResult(exit_code=0, stdout="", stderr="")

        monkeypatch.setattr(provider, "_run_command", _fake_run)

        ref = await provider.create_workspace(spec)

        assert ref in provider._workspaces
        assert clone_calls == [(["git", "clone", "https://github.com/acme/app", "."], provider._workspaces[ref])]
        await provider.destroy_workspace(ref)

    async def test_create_workspace_clone_failure_cleans_up(
        self, provider: LocalRuntimeProvider, spec: WorkspaceSpec, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        spec.labels = {"repo_url": "https://github.com/acme/app"}
        rmtree = MagicMock()
        monkeypatch.setattr("modulo.core.runtime_provider.local.shutil.rmtree", rmtree)

        async def _fake_run(command: list[str], cwd: str, cmd_timeout: int | None) -> ExecResult:
            raise RuntimeError("clone failed")

        monkeypatch.setattr(provider, "_run_command", _fake_run)

        with pytest.raises(RuntimeError, match="clone failed"):
            await provider.create_workspace(spec)

        assert provider._workspaces == {}
        rmtree.assert_called_once()

    async def test_destroy_workspace_logs_cleanup_failure(
        self,
        provider: LocalRuntimeProvider,
        spec: WorkspaceSpec,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        ref = await provider.create_workspace(spec)

        def _rmtree_fails(path: str, ignore_errors: bool = False) -> None:
            raise OSError("cannot remove")

        monkeypatch.setattr("modulo.core.runtime_provider.local.shutil.rmtree", _rmtree_fails)

        with caplog.at_level("ERROR", logger="modulo.core.runtime_provider.local"):
            await provider.destroy_workspace(ref)

        assert "Failed to remove workspace" in caplog.text
        assert ref not in provider._workspaces

    async def test_destroy_workspace_cancellation_propagates(
        self, provider: LocalRuntimeProvider, spec: WorkspaceSpec, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ref = await provider.create_workspace(spec)

        async def _cancelled(*args: object, **kwargs: object) -> object:
            raise asyncio.CancelledError

        monkeypatch.setattr(asyncio, "to_thread", _cancelled)

        with pytest.raises(asyncio.CancelledError):
            await provider.destroy_workspace(ref)
        assert ref not in provider._workspaces
