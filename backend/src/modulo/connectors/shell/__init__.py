"""ShellConnector — execute commands and manage files in a workspace via RuntimeProvider.

Pass ``provider_ref`` in query.filters or payload.data to target the correct
workspace.  The calling layer must ensure an active WorkspaceLease exists before
invoking this connector (403 otherwise).
"""

import base64
import shlex
from pathlib import Path
from typing import Any

from modulo.connectors.base import (
    ConnectorBase,
    ConnectorPayload,
    ConnectorPermissionError,
    ConnectorQuery,
    ConnectorResult,
    ConnectorType,
    HealthResult,
)
from modulo.core.runtime_provider import RuntimeProvider


class ShellConnector(ConnectorBase):
    """Execute shell commands and manage files inside a workspace lease.

    Supported query resources:
      "file"      — read a file via ``cat``; filters: {path, provider_ref}
      "directory" — list a directory via ``ls``; filters: {path, provider_ref}

    Supported write resources:
      "command"   — run a command with allowlist enforcement;
                    data: {command, cwd?, env?, timeout_seconds?, provider_ref}
      "file"      — write base64-encoded content to a file;
                    data: {path, content, provider_ref}
    """

    def __init__(
        self,
        runtime_provider: RuntimeProvider | None = None,
        allowed_commands: list[str] | None = None,
    ) -> None:
        self._runtime_provider = runtime_provider
        self._allowed_commands = frozenset(allowed_commands or [])

    @property
    def connector_type(self) -> ConnectorType:
        return ConnectorType.SHELL

    async def health_check(self) -> HealthResult:
        if self._runtime_provider is None:
            return HealthResult(ok=False, detail="Runtime provider not configured")
        return HealthResult(ok=True, detail="ShellConnector ready")

    def _check_command_allowed(self, command: list[str]) -> None:
        if not self._allowed_commands:
            raise ConnectorPermissionError(
                "No commands are allowed (deny-all). "
                "Operator must configure permitted commands in the environment profile."
            )
        base = command[0] if command else ""
        if base not in self._allowed_commands:
            raise ConnectorPermissionError(
                f"Command {base!r} is not in the allowed list: {sorted(self._allowed_commands)}"
            )

    async def query(self, q: ConnectorQuery) -> ConnectorResult:
        if self._runtime_provider is None:
            raise ValueError("Runtime provider not configured")
        provider_ref: str | None = q.filters.get("provider_ref")
        if not provider_ref:
            raise ValueError("provider_ref is required in query filters")

        match q.resource:
            case "file":
                path = q.filters["path"]
                safe_path = shlex.quote(path)
                result = await self._runtime_provider.exec_command(
                    provider_ref, ["sh", "-c", f"cat {safe_path}"], timeout=30
                )
                if result.exit_code != 0:
                    raise ValueError(f"Failed to read file {path!r}: {result.stderr.strip()}")
                return ConnectorResult(records=[{"path": path, "content": result.stdout}])

            case "directory":
                dir_path = q.filters.get("path", ".")
                safe_path = shlex.quote(dir_path)
                result = await self._runtime_provider.exec_command(
                    provider_ref, ["sh", "-c", f"ls -1a {safe_path}"], timeout=30
                )
                entries: list[dict[str, Any]] = []
                for line in result.stdout.strip().split("\n"):
                    name = line.strip()
                    if name and name not in (".", ".."):
                        resolved = f"{dir_path.rstrip('/')}/{name}"
                        entries.append({"name": name, "path": resolved})
                return ConnectorResult(records=entries, total=len(entries))

            case _:
                raise ValueError(f"Unsupported shell query resource: {q.resource!r}")

    async def write(self, payload: ConnectorPayload) -> dict[str, Any]:
        if self._runtime_provider is None:
            raise ValueError("Runtime provider not configured")
        provider_ref: str | None = payload.data.get("provider_ref")
        if not provider_ref:
            raise ValueError("provider_ref is required in payload data")

        match payload.resource:
            case "command":
                command_str: str = payload.data["command"]
                command_parts = shlex.split(command_str)
                self._check_command_allowed(command_parts)

                env: dict[str, str] | None = payload.data.get("env")
                timeout: int = payload.data.get("timeout_seconds", 60)
                cwd: str | None = payload.data.get("cwd")

                cmd = self._build_exec_cmd(command_str, cwd, env)
                exec_result = await self._runtime_provider.exec_command(
                    provider_ref,
                    cmd,
                    timeout=timeout,
                )
                return {
                    "stdout": exec_result.stdout,
                    "stderr": exec_result.stderr,
                    "exit_code": exec_result.exit_code,
                    "duration_ms": exec_result.duration_ms,
                    "masked": True,
                }

            case "file":
                path: str = payload.data["path"]
                content: str = payload.data["content"]
                safe_path = shlex.quote(path)
                encoded = base64.b64encode(content.encode()).decode()
                parent = str(Path(path).parent)
                safe_parent = shlex.quote(parent)

                exec_result = await self._runtime_provider.exec_command(
                    provider_ref,
                    [
                        "sh",
                        "-c",
                        f"mkdir -p {safe_parent} && echo '{encoded}' | base64 -d > {safe_path}",
                    ],
                    timeout=30,
                )
                if exec_result.exit_code != 0:
                    raise ValueError(f"Failed to write file {path!r}: {exec_result.stderr.strip()}")
                return {
                    "path": path,
                    "bytes_written": len(content),
                    "exit_code": exec_result.exit_code,
                }

            case _:
                raise ValueError(f"Unsupported shell write resource: {payload.resource!r}")

    def _build_exec_cmd(
        self,
        command_str: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> list[str]:
        """Build the exec_command arg list, wrapping with cd/env as needed.

        When cwd or env is set, we invoke ``sh -c`` with each token individually
        quoted so that shell metacharacters (``;``, ``&&``, ``|``, etc.) in
        ``command_str`` cannot bypass the allowlist.
        """
        command_parts = shlex.split(command_str)
        if not cwd and not env:
            return command_parts

        shell_parts: list[str] = []
        if cwd:
            shell_parts.append(f"cd {shlex.quote(cwd)}")
        quoted_cmd = " ".join(shlex.quote(p) for p in command_parts)
        if env:
            env_prefix = " ".join(f"{shlex.quote(k)}={shlex.quote(v)}" for k, v in env.items())
            quoted_cmd = f"{env_prefix} {quoted_cmd}"
        shell_parts.append(quoted_cmd)
        return ["sh", "-c", " && ".join(shell_parts)]
