"""Shell connector contract tests — real subprocess execution (no Docker).

Exercises the ShellConnector with a real ``SubprocessRuntimeProvider`` that
executes commands via ``asyncio.create_subprocess_exec``.  No Docker, no
mocks — real stdout/exit-code assertions.

Note: some tests are platform-dependent (``ls`` is unavailable on vanilla
Windows).  These tests use only cross-platform commands (``echo``,
``python``).
"""

import asyncio
import warnings
from typing import Any

import pytest

from modulo.connectors.base import ConnectorPayload, ConnectorType
from tests.connectors._conformance import assert_health_shape


class SubprocessRuntimeProvider:
    """A real runtime provider that executes commands via the OS."""

    async def execute_command(
        self,
        workspace: Any,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_seconds: int = 60,
    ) -> dict[str, Any]:
        import shlex

        parts = shlex.split(command)
        proc = await asyncio.create_subprocess_exec(
            *parts,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=env,
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
        except TimeoutError:
            proc.kill()
            await asyncio.wait_for(proc.wait(), timeout=5)
            return {"exit_code": -1, "stdout": "", "stderr": "timeout"}

        return {
            "exit_code": proc.returncode,
            "stdout": stdout_bytes.decode("utf-8", errors="replace"),
            "stderr": stderr_bytes.decode("utf-8", errors="replace"),
        }


@pytest.fixture
def real_shell_connector():
    """ShellConnector backed by a real subprocess runtime provider."""
    from modulo.connectors.shell import ShellConnector

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        return ShellConnector(
            runtime_provider=SubprocessRuntimeProvider(),
            allowed_commands=["echo", "python", "cat", "ls", "cmd"],
        )


class TestShellRealSubprocess:
    def test_connector_type(self, real_shell_connector) -> None:
        assert real_shell_connector.connector_type == ConnectorType.SHELL

    async def test_health_check_ok(self, real_shell_connector) -> None:
        result = await real_shell_connector.health_check()
        assert_health_shape(result)
        assert result.ok is True

    async def test_write_command_real_stdout(self, real_shell_connector) -> None:
        """Execute a command and assert on real stdout."""
        result = await real_shell_connector.write(
            ConnectorPayload(
                resource="command",
                data={"command": "python -c \"print('hello-world-from-shell-connector')\""},
            )
        )
        assert result["exit_code"] == 0
        assert "hello-world-from-shell-connector" in result["stdout"]

    async def test_write_command_nonzero_exit(self, real_shell_connector) -> None:
        """A failing command produces a non-zero exit code."""
        result = await real_shell_connector.write(
            ConnectorPayload(
                resource="command",
                data={"command": 'python -c "import sys; sys.exit(42)"'},
            )
        )
        assert result["exit_code"] == 42

    async def test_write_command_blocked(self, real_shell_connector) -> None:
        """Command not in allowlist raises ConnectorPermissionError."""
        from modulo.connectors.base import ConnectorPermissionError

        with pytest.raises(ConnectorPermissionError, match="not in the allowed list"):
            await real_shell_connector.write(
                ConnectorPayload(
                    resource="command",
                    data={"command": "rm -rf /"},
                )
            )

    async def test_health_check_no_provider(self) -> None:
        """ShellConnector with no provider reports not-ok."""
        from modulo.connectors.shell import ShellConnector

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            connector = ShellConnector(runtime_provider=None)
        result = await connector.health_check()
        assert_health_shape(result)
        assert result.ok is False

    async def test_write_command_stdout_stderr(self, real_shell_connector) -> None:
        """Command that writes to both stdout and stderr."""
        result = await real_shell_connector.write(
            ConnectorPayload(
                resource="command",
                data={"command": "python -c \"import sys; sys.stdout.write('out'); sys.stderr.write('err')\""},
            )
        )
        assert result["exit_code"] == 0
        assert "out" in result["stdout"]
        assert "err" in result["stderr"]

    async def test_write_returns_masked_flag(self, real_shell_connector) -> None:
        """Write result includes masked=True flag."""
        result = await real_shell_connector.write(
            ConnectorPayload(
                resource="command",
                data={"command": "python -c \"print('test')\""},
            )
        )
        assert result.get("masked") is True

    async def test_health_check_with_provider(self, real_shell_connector) -> None:
        """Health check with a configured provider returns ok."""
        result = await real_shell_connector.health_check()
        assert result.ok is True
