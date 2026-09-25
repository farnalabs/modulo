"""Shared pytest fixtures for the pipeline_engine unit test suite."""

import os

# The backend ``Settings`` model requires DATABASE_URL/SECRET_KEY/FERNET_KEY
# and the D8 gate reads ``get_settings()`` on every dispatch path. There is no
# ``.env`` in worktrees, so provide the minimum env the same way as
# ``tests/unit/core/conftest.py`` — setdefault so explicit CI values always
# win. Without this, running THIS directory standalone (not after core/) fails
# on the first ``get_settings()`` inside the dispatch gate.
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://localhost/test")
os.environ.setdefault("SECRET_KEY", "a" * 32)
os.environ.setdefault("FERNET_KEY", "a" * 32)
os.environ.setdefault("REDIS_URL", "")
os.environ.setdefault("MODULO_ADMIN_PASSWORD", "test")
os.environ.setdefault("MODULO_CSRF_ENABLED", "false")

from typing import Any
from unittest.mock import AsyncMock

import pytest
from langgraph.errors import GraphInterrupt
from langgraph.types import Interrupt

from modulo.core.runtime_provider import (
    ExecProcess,
    ExecResult,
    ExecStreamChunk,
    RuntimeProvider,
    WorkspaceFileInfo,
    WorkspaceSpec,
)


class FakeFileIOProvider(RuntimeProvider):
    """In-memory stand-in for the FAR-1050 R2b flag-ON file-I/O primitives.

    Stores bytes keyed by path — bytes-in/bytes-out, exactly the ABC
    contract — and records every operation on ``events`` so a test can assert
    routing (which primitive ran) and ordering (writes land before the agent
    command). ``write_error`` arms the write path with a failure, mirroring a
    legacy ``sandbox.files.write`` side_effect on the flag-OFF path.
    """

    provider_id = "e2b"

    def __init__(self, files: dict[str, bytes] | None = None) -> None:
        self.files: dict[str, bytes] = dict(files or {})
        self.events: list[str] = []
        self.refs: list[str] = []
        self.write_error: BaseException | None = None

    async def create_workspace(self, spec: WorkspaceSpec) -> str:
        return "ws-fake"

    async def exec_command(
        self,
        provider_ref: str,
        command: list[str],
        *,
        cmd_timeout: int | None = None,
    ) -> ExecResult:
        return ExecResult(exit_code=0, stdout="", stderr="")

    async def destroy_workspace(self, provider_ref: str) -> None:
        return None

    async def get_workspace_status(self, provider_ref: str) -> str:
        return "running"

    async def write_file(self, provider_ref: str, path: str, data: bytes) -> None:
        self.refs.append(provider_ref)
        self.events.append(f"write:{path}")
        if self.write_error is not None:
            raise self.write_error
        self.files[path] = bytes(data)

    async def read_file(self, provider_ref: str, path: str) -> bytes:
        self.refs.append(provider_ref)
        self.events.append(f"read:{path}")
        return bytes(self.files.get(path, b""))

    async def list_files(self, provider_ref: str, path: str) -> list[str]:
        self.refs.append(provider_ref)
        self.events.append(f"list:{path}")
        return sorted(self.files)

    async def get_info(self, provider_ref: str, path: str) -> WorkspaceFileInfo:
        self.refs.append(provider_ref)
        self.events.append(f"get_info:{path}")
        data = self.files.get(path)
        return WorkspaceFileInfo(path=path, size=len(data) if data is not None else 0, is_dir=False)


class FakeDispatchProvider(RuntimeProvider):
    """FAR-1050 R4 flag-ON dispatch stand-in: create / stream / kill.

    Records the ABC call sequence on ``events`` (``create``, ``command``,
    ``exec``, ``destroy``, ``destroy_by_ref``, ``close``) so a test can assert
    the whole dispatch ran through the provider, and yields a scripted
    ``ExecProcess`` whose chunks / exit code / stream error the test controls.
    ``stream_start_exception`` lets a test fail the command START (the
    dispatch maps that ``TimeoutError`` onto its stall/timeout arm exactly as
    the legacy background-command start bound does).

    ``command_events`` / ``kill_events`` are optional external lists the fake
    appends to — used by tests that interleave the dispatch's own milestones
    (the pre-kill log-tail ordering probe, the write-before-command probe)
    with a list they already own.
    """

    provider_id = "e2b"

    def __init__(
        self,
        *,
        ref: str = "sbx-dispatch",
        exit_code: int = 1,
        chunks: list[ExecStreamChunk] | None = None,
        stream_error: str | None = None,
        stream_start_exception: BaseException | None = None,
        create_exception: BaseException | None = None,
        command_events: list[str] | None = None,
        kill_events: list[str] | None = None,
    ) -> None:
        self.ref = ref
        self.exit_code = exit_code
        self.chunks = list(chunks or [])
        self.stream_error = stream_error
        self.stream_start_exception = stream_start_exception
        self.create_exception = create_exception
        self.command_events = command_events
        self.kill_events = kill_events
        self.events: list[str] = []
        self.kills: list[str] = []
        self.created_spec: WorkspaceSpec | None = None
        self.last_command: list[str] | None = None
        self.last_environment: dict[str, str] | None = None

    async def create_workspace(self, spec: WorkspaceSpec) -> str:
        self.events.append("create")
        self.created_spec = spec
        if self.create_exception is not None:
            raise self.create_exception
        return self.ref

    async def exec_command(
        self,
        provider_ref: str,
        command: list[str],
        *,
        cmd_timeout: int | None = None,
    ) -> ExecResult:
        self.events.append("exec")
        self.last_command = list(command)
        return ExecResult(exit_code=0, stdout="", stderr="")

    async def exec_command_stream(
        self,
        provider_ref: str,
        command: list[str],
        *,
        environment: dict[str, str] | None = None,
    ) -> ExecProcess:
        if self.command_events is not None:
            self.command_events.append("command")
        self.events.append("command")
        self.last_command = list(command)
        self.last_environment = dict(environment or {})
        if self.stream_start_exception is not None:
            raise self.stream_start_exception

        chunks = list(self.chunks)
        stream_error = self.stream_error
        exit_code = self.exit_code
        process = ExecProcess(chunks=None, kill=None)  # type: ignore[arg-type]

        async def _chunks() -> Any:
            try:
                for chunk in chunks:
                    yield chunk
                if stream_error is not None:
                    # A stream drop: ``exit_code`` stays None (no fabricated
                    # zero), ``error`` carries the description.
                    process.error = stream_error
                    process.exit_code = None
                else:
                    process.exit_code = exit_code
            finally:
                process.done.set()

        async def _kill() -> None:
            self.kills.append("stream_kill")

        process.chunks = _chunks()
        process._kill = _kill
        return process

    async def destroy_workspace(self, provider_ref: str) -> None:
        self.events.append("destroy")
        self.kills.append("destroy")
        if self.kill_events is not None:
            self.kill_events.append("kill")

    async def destroy_workspace_by_ref(self, provider_ref: str) -> bool:
        self.events.append("destroy_by_ref")
        self.kills.append("destroy_by_ref")
        if self.kill_events is not None:
            self.kill_events.append("kill")
        return True

    async def get_workspace_status(self, provider_ref: str) -> str:
        return "running"

    async def close(self) -> None:
        self.events.append("close")


def install_fake_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    *,
    ref: str = "sbx-dispatch",
    **kwargs: Any,
) -> FakeDispatchProvider:
    """Route the FAR-1050 R4 dispatch seam (``_build_dispatch_provider``) to a fake.

    Opt-in: only a flag-ON dispatch test needs it. Without it the real seam
    builds a hub (and, with a legacy ``E2B_API_KEY`` set, a REAL E2B provider
    that would attempt a network ``AsyncSandbox.create``).
    """
    provider = FakeDispatchProvider(ref=ref, **kwargs)
    monkeypatch.setattr(
        "modulo.core.pipeline_engine.node_runner._build_dispatch_provider",
        AsyncMock(return_value=provider),
    )
    return provider


@pytest.fixture
def fake_file_io(monkeypatch: pytest.MonkeyPatch) -> FakeFileIOProvider:
    """Route node_runner's R2b file-I/O seam (``_build_file_io_provider``) to a fake.

    Opt-in: only a test that turns the flag ON needs it. Without it the real
    builder would construct an E2B provider and try to ``AsyncSandbox.connect``
    (a network call) for a sandbox id that only exists in the mock.
    """
    provider = FakeFileIOProvider()
    monkeypatch.setenv("E2B_API_KEY", "test-key")
    monkeypatch.setattr(
        "modulo.core.pipeline_engine.node_runner._build_file_io_provider",
        AsyncMock(return_value=provider),
    )
    return provider


@pytest.fixture
def _interrupt_without_graph_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulate Interrupt() outside a LangGraph runtime (raises GraphInterrupt).

    Opt-in (not autouse) so tests that exercise the real interrupt machinery
    (e.g. test_executor.py) keep their real-interrupt expectations.
    """

    def raise_interrupt(value: Any) -> None:
        raise GraphInterrupt((Interrupt(value=value),))

    monkeypatch.setattr("modulo.core.pipeline_engine.node_runner.interrupt", raise_interrupt)
