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

from modulo.core.runtime_provider import ExecResult, RuntimeProvider, WorkspaceFileInfo, WorkspaceSpec


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
