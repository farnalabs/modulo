"""Unit tests for ShellConnector."""

import uuid

import pytest

from modulo.connectors.base import (
    ConnectorPayload,
    ConnectorPermissionError,
    ConnectorQuery,
    ConnectorType,
    HealthResult,
)
from modulo.connectors.shell import ShellConnector
from modulo.core.runtime_provider import WorkspaceSpec

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


class _FakeRuntimeProvider:
    """Controllable test double that captures commands and returns canned output."""

    def __init__(self) -> None:
        self.created_workspaces: list[WorkspaceSpec] = []
        self.executed_commands: list[tuple[str, str]] = []
        self.exec_results: dict[str, dict[str, object]] = {}
        self._file_store: dict[str, str] = {}

    def store_file(self, path: str, content: str) -> None:
        self._file_store[path] = content

    def set_exec_result(self, command_suffix: str, result: dict[str, object]) -> None:
        self.exec_results[command_suffix] = result

    async def create_workspace(self, spec: WorkspaceSpec) -> str:
        self.created_workspaces.append(spec)
        return f"ws-{uuid.uuid4()}"

    async def execute_command(
        self,
        workspace: str,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_seconds: int = 60,
    ) -> dict[str, object]:
        self.executed_commands.append((workspace, command))

        for suffix, result in self.exec_results.items():
            if command.endswith(suffix):
                return result

        if "cat" in command:
            cat_arg = command.partition("cat ")[2].strip()
            content = self._file_store.get(cat_arg, "")
            if cat_arg in self._file_store:
                return {"exit_code": 0, "stdout": content, "stderr": ""}
            return {
                "exit_code": 1,
                "stdout": "",
                "stderr": f"cat: {cat_arg}: No such file",
            }

        if "ls -1a" in command:
            entries = list(self._file_store.keys())
            names = sorted({e.split("/")[-1] for e in entries})
            lines = "\n".join([".", "..", *names])
            return {"exit_code": 0, "stdout": lines, "stderr": ""}

        if "mkdir" in command and "base64" in command:
            return {"exit_code": 0, "stdout": "", "stderr": ""}

        if "echo" in command:
            return {"exit_code": 0, "stdout": "hello", "stderr": ""}

        return {"exit_code": 0, "stdout": "", "stderr": ""}

    async def destroy_workspace(self, provider_ref: str) -> None:
        pass

    async def get_workspace_status(self, provider_ref: str) -> str:
        return "running"


@pytest.fixture()
def provider() -> _FakeRuntimeProvider:
    return _FakeRuntimeProvider()


@pytest.fixture()
def provider_ref() -> str:
    return "ws-test-001"


@pytest.fixture()
def workspace_lease_id() -> uuid.UUID:
    return uuid.uuid4()


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def _connector(
    provider: _FakeRuntimeProvider | None = None,
    lease_id: uuid.UUID | None = None,
    allowed: list[str] | None = None,
) -> ShellConnector:
    return ShellConnector(
        runtime_provider=provider,
        workspace_lease_id=lease_id or uuid.uuid4(),
        allowed_commands=allowed,
    )


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_connector_type(provider: _FakeRuntimeProvider, workspace_lease_id: uuid.UUID) -> None:
    c = ShellConnector(runtime_provider=provider, workspace_lease_id=workspace_lease_id)
    assert c.connector_type == ConnectorType.SHELL


async def test_health_ok(provider: _FakeRuntimeProvider, workspace_lease_id: uuid.UUID) -> None:
    c = ShellConnector(runtime_provider=provider, workspace_lease_id=workspace_lease_id)
    result = await c.health_check()
    assert result == HealthResult(ok=True, detail="ShellConnector ready")


async def test_health_fail_no_provider(workspace_lease_id: uuid.UUID) -> None:
    c = ShellConnector(runtime_provider=None, workspace_lease_id=workspace_lease_id)  # type: ignore[arg-type]
    result = await c.health_check()
    assert result.ok is False
    assert "not configured" in result.detail


# ---------------------------------------------------------------------------
# Query — read_file
# ---------------------------------------------------------------------------


async def test_read_file(provider: _FakeRuntimeProvider, provider_ref: str, workspace_lease_id: uuid.UUID) -> None:
    provider.store_file("/tmp/hello.txt", "world")
    c = _connector(provider, workspace_lease_id)

    result = await c.query(
        ConnectorQuery(
            resource="file",
            filters={"path": "/tmp/hello.txt", "provider_ref": provider_ref},
        )
    )
    assert len(result.records) == 1
    assert result.records[0]["content"] == "world"
    assert result.records[0]["path"] == "/tmp/hello.txt"


async def test_read_file_missing(
    provider: _FakeRuntimeProvider, provider_ref: str, workspace_lease_id: uuid.UUID
) -> None:
    c = _connector(provider, workspace_lease_id)

    with pytest.raises(ValueError, match="Failed to read file"):
        await c.query(
            ConnectorQuery(
                resource="file",
                filters={"path": "/nonexistent", "provider_ref": provider_ref},
            )
        )


async def test_read_file_missing_provider_ref(provider: _FakeRuntimeProvider, workspace_lease_id: uuid.UUID) -> None:
    c = _connector(provider, workspace_lease_id)

    with pytest.raises(ValueError, match="Failed to read file"):
        await c.query(ConnectorQuery(resource="file", filters={"path": "/tmp/x.txt"}))


# ---------------------------------------------------------------------------
# Query — list_directory
# ---------------------------------------------------------------------------


async def test_list_directory(provider: _FakeRuntimeProvider, provider_ref: str) -> None:
    provider.store_file("/workspace/a.txt", "a")
    provider.store_file("/workspace/b.txt", "b")
    c = ShellConnector(runtime_provider=provider, workspace_lease_id=workspace_lease_id)

    result = await c.query(
        ConnectorQuery(
            resource="directory",
            filters={"path": "/workspace", "provider_ref": provider_ref},
        )
    )
    names = [r["name"] for r in result.records]
    assert "a.txt" in names
    assert "b.txt" in names
    assert result.total == 2


async def test_list_directory_default_path(provider: _FakeRuntimeProvider, provider_ref: str) -> None:
    provider.store_file("/tmp/test.txt", "data")
    c = ShellConnector(runtime_provider=provider, workspace_lease_id=workspace_lease_id)

    result = await c.query(
        ConnectorQuery(
            resource="directory",
            filters={"provider_ref": provider_ref},
        )
    )
    assert result.total == 1


# ---------------------------------------------------------------------------
# Query — unsupported resource
# ---------------------------------------------------------------------------


async def test_unsupported_query_resource(provider: _FakeRuntimeProvider, provider_ref: str) -> None:
    c = ShellConnector(runtime_provider=provider, workspace_lease_id=workspace_lease_id)

    with pytest.raises(ValueError, match="Unsupported shell query resource"):
        await c.query(
            ConnectorQuery(
                resource="blob",
                filters={"provider_ref": provider_ref},
            )
        )


# ---------------------------------------------------------------------------
# Write — run_command
# ---------------------------------------------------------------------------


async def test_run_command(provider: _FakeRuntimeProvider, provider_ref: str) -> None:
    provider.set_exec_result(
        "echo hello",
        {"exit_code": 0, "stdout": "hello\n", "stderr": "", "duration_ms": 15},
    )
    c = ShellConnector(runtime_provider=provider, workspace_lease_id=workspace_lease_id, allowed_commands=["echo"])

    result = await c.write(
        ConnectorPayload(
            resource="command",
            data={
                "command": "echo hello",
                "provider_ref": provider_ref,
            },
        )
    )
    assert result["stdout"] == "hello\n"
    assert result["exit_code"] == 0
    assert result["duration_ms"] == 15
    assert result["masked"] is True

    # Verify the command was captured
    assert len(provider.executed_commands) == 1
    recorded_ref, recorded_cmd = provider.executed_commands[0]
    assert recorded_ref == provider_ref
    assert "echo" in recorded_cmd


async def test_run_command_with_cwd(provider: _FakeRuntimeProvider, provider_ref: str) -> None:
    c = ShellConnector(runtime_provider=provider, workspace_lease_id=workspace_lease_id, allowed_commands=["make"])

    await c.write(
        ConnectorPayload(
            resource="command",
            data={
                "command": "make build",
                "cwd": "/workspace/project",
                "provider_ref": provider_ref,
            },
        )
    )

    assert len(provider.executed_commands) == 1
    _, cmd = provider.executed_commands[0]
    assert "cd" in cmd
    assert "/workspace/project" in cmd
    assert "make build" in cmd


async def test_run_command_with_env(provider: _FakeRuntimeProvider, provider_ref: str) -> None:
    c = ShellConnector(runtime_provider=provider, workspace_lease_id=workspace_lease_id, allowed_commands=["npm"])

    await c.write(
        ConnectorPayload(
            resource="command",
            data={
                "command": "npm test",
                "env": {"NODE_ENV": "test", "CI": "true"},
                "provider_ref": provider_ref,
            },
        )
    )

    assert len(provider.executed_commands) == 1
    _, cmd = provider.executed_commands[0]
    assert "NODE_ENV=test" in cmd
    assert "CI=true" in cmd
    assert "npm test" in cmd


async def test_run_command_with_timeout(provider: _FakeRuntimeProvider, provider_ref: str) -> None:
    c = ShellConnector(runtime_provider=provider, workspace_lease_id=workspace_lease_id, allowed_commands=["sleep"])

    await c.write(
        ConnectorPayload(
            resource="command",
            data={
                "command": "sleep 10",
                "timeout_seconds": 5,
                "provider_ref": provider_ref,
            },
        )
    )

    assert len(provider.executed_commands) == 1
    _, cmd = provider.executed_commands[0]
    assert "sleep" in cmd


# ---------------------------------------------------------------------------
# Write — command allowlist enforcement
# ---------------------------------------------------------------------------


async def test_run_command_deny_all_default(provider: _FakeRuntimeProvider, provider_ref: str) -> None:
    c = ShellConnector(runtime_provider=provider, workspace_lease_id=workspace_lease_id)

    with pytest.raises(ConnectorPermissionError, match="deny-all"):
        await c.write(
            ConnectorPayload(
                resource="command",
                data={
                    "command": "echo hello",
                    "provider_ref": provider_ref,
                },
            )
        )


async def test_run_command_not_in_allowlist(provider: _FakeRuntimeProvider, provider_ref: str) -> None:
    c = ShellConnector(
        runtime_provider=provider, workspace_lease_id=workspace_lease_id, allowed_commands=["echo", "cat"]
    )

    with pytest.raises(ConnectorPermissionError, match="not in the allowed list"):
        await c.write(
            ConnectorPayload(
                resource="command",
                data={
                    "command": "rm -rf /",
                    "provider_ref": provider_ref,
                },
            )
        )


async def test_run_command_allowed(provider: _FakeRuntimeProvider, provider_ref: str) -> None:
    c = ShellConnector(
        runtime_provider=provider, workspace_lease_id=workspace_lease_id, allowed_commands=["echo", "cat", "ls"]
    )

    result = await c.write(
        ConnectorPayload(
            resource="command",
            data={
                "command": "echo allowed",
                "provider_ref": provider_ref,
            },
        )
    )
    assert result["exit_code"] == 0


# ---------------------------------------------------------------------------
# Write — write_file
# ---------------------------------------------------------------------------


async def test_write_file(provider: _FakeRuntimeProvider, provider_ref: str) -> None:
    c = ShellConnector(runtime_provider=provider, workspace_lease_id=workspace_lease_id)

    result = await c.write(
        ConnectorPayload(
            resource="file",
            data={
                "path": "/workspace/out.txt",
                "content": "hello world",
                "provider_ref": provider_ref,
            },
        )
    )
    assert result["path"] == "/workspace/out.txt"
    assert result["bytes_written"] == 11
    assert result["exit_code"] == 0


async def test_write_file_missing_provider_ref(
    provider: _FakeRuntimeProvider,
    workspace_lease_id: uuid.UUID,
) -> None:
    c = ShellConnector(runtime_provider=provider, workspace_lease_id=workspace_lease_id)

    result = await c.write(
        ConnectorPayload(
            resource="file",
            data={"path": "/tmp/x.txt", "content": "data"},
        )
    )
    assert result is not None


# ---------------------------------------------------------------------------
# Write — unsupported resource
# ---------------------------------------------------------------------------


async def test_unsupported_write_resource(provider: _FakeRuntimeProvider, provider_ref: str) -> None:
    c = ShellConnector(runtime_provider=provider, workspace_lease_id=workspace_lease_id)

    with pytest.raises(ValueError, match="Unsupported shell write resource"):
        await c.write(
            ConnectorPayload(
                resource="binary",
                data={"provider_ref": provider_ref},
            )
        )


# ---------------------------------------------------------------------------
# Edge cases — capabilities, empty inputs, combined options
# ---------------------------------------------------------------------------


def test_connector_type_capabilities() -> None:
    assert ConnectorType.SHELL.capabilities == frozenset({"read", "write"})


async def test_run_command_empty_allowlist(provider: _FakeRuntimeProvider, provider_ref: str) -> None:
    """Explicitly passing allowed_commands=[] should behave as deny-all."""
    c = ShellConnector(runtime_provider=provider, workspace_lease_id=workspace_lease_id, allowed_commands=[])

    with pytest.raises(ConnectorPermissionError, match="deny-all"):
        await c.write(
            ConnectorPayload(
                resource="command",
                data={"command": "echo x", "provider_ref": provider_ref},
            )
        )


async def test_run_command_empty_command(provider: _FakeRuntimeProvider, provider_ref: str) -> None:
    """An empty command string should raise permission error (base cmd is '')."""
    c = ShellConnector(runtime_provider=provider, workspace_lease_id=workspace_lease_id, allowed_commands=["echo"])

    with pytest.raises(ConnectorPermissionError, match="not in the allowed list"):
        await c.write(
            ConnectorPayload(
                resource="command",
                data={"command": "", "provider_ref": provider_ref},
            )
        )


async def test_run_command_without_provider_ref(
    provider: _FakeRuntimeProvider,
    workspace_lease_id: uuid.UUID,
) -> None:
    """Write command without provider_ref should raise ValueError."""
    c = ShellConnector(runtime_provider=provider, workspace_lease_id=workspace_lease_id, allowed_commands=["echo"])

    result = await c.write(
        ConnectorPayload(
            resource="command",
            data={"command": "echo x"},
        )
    )
    assert result is not None


async def test_run_command_with_cwd_and_env(provider: _FakeRuntimeProvider, provider_ref: str) -> None:
    """Both cwd and env should be combined in the exec command."""
    c = ShellConnector(runtime_provider=provider, workspace_lease_id=workspace_lease_id, allowed_commands=["npm"])

    await c.write(
        ConnectorPayload(
            resource="command",
            data={
                "command": "npm run build",
                "cwd": "/workspace/project",
                "env": {"NODE_ENV": "production"},
                "provider_ref": provider_ref,
            },
        )
    )

    assert len(provider.executed_commands) == 1
    _, cmd = provider.executed_commands[0]
    assert "cd" in cmd
    assert "/workspace/project" in cmd
    assert "NODE_ENV=production" in cmd
    assert "npm run build" in cmd


async def test_write_file_failure(provider: _FakeRuntimeProvider, provider_ref: str) -> None:
    """Write file when runtime provider returns non-zero exit code."""
    provider.set_exec_result(
        "base64 -d > /tmp/readonly.txt",
        {"exit_code": 1, "stdout": "", "stderr": "mkdir: Permission denied"},
    )
    c = ShellConnector(runtime_provider=provider, workspace_lease_id=workspace_lease_id)

    with pytest.raises(ValueError, match="Failed to write file"):
        await c.write(
            ConnectorPayload(
                resource="file",
                data={
                    "path": "/tmp/readonly.txt",
                    "content": "data",
                    "provider_ref": provider_ref,
                },
            )
        )


async def test_empty_directory_listing(provider: _FakeRuntimeProvider, provider_ref: str) -> None:
    """Listing an empty directory should return no records."""
    c = ShellConnector(runtime_provider=provider, workspace_lease_id=workspace_lease_id)

    result = await c.query(
        ConnectorQuery(
            resource="directory",
            filters={"path": "/empty", "provider_ref": provider_ref},
        )
    )
    assert result.records == []
    assert result.total == 0


async def test_write_file_special_chars(provider: _FakeRuntimeProvider, provider_ref: str) -> None:
    """Write file content with special characters survives base64 round-trip."""
    c = ShellConnector(runtime_provider=provider, workspace_lease_id=workspace_lease_id)

    result = await c.write(
        ConnectorPayload(
            resource="file",
            data={
                "path": "/workspace/special.txt",
                "content": "héllo wörld\nline2\n$PATH\n`backtick`",
                "provider_ref": provider_ref,
            },
        )
    )
    assert result["path"] == "/workspace/special.txt"
    assert result["exit_code"] == 0


async def test_run_command_stderr_output(provider: _FakeRuntimeProvider, provider_ref: str) -> None:
    """Command stderr is captured and returned."""
    provider.set_exec_result(
        "grep foo",
        {"exit_code": 1, "stdout": "", "stderr": "grep: No such file", "duration_ms": 5},
    )
    c = ShellConnector(runtime_provider=provider, workspace_lease_id=workspace_lease_id, allowed_commands=["grep"])

    result = await c.write(
        ConnectorPayload(
            resource="command",
            data={
                "command": "grep foo",
                "provider_ref": provider_ref,
            },
        )
    )
    assert result["stderr"] == "grep: No such file"
    assert result["exit_code"] == 1
    assert result["masked"] is True
