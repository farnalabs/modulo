"""ShellConnector — execute commands and manage files in a workspace via RuntimeProvider.

.. deprecated::
    ShellConnector is deprecated since ADR 003 (2026-07-16).  The Modulo agent
    execution environment model (ADR 001) has been superseded by the Agent
    Dispatch Model.  Modulo no longer runs agents inside sandboxes — it
    dispatches work to external agent runtimes via the ``sandbox_agent`` node
    type.  ShellConnector will be removed in a future release.

Pass ``provider_ref`` in query.filters or payload.data to target the correct
workspace.  The calling layer must ensure an active WorkspaceLease exists before
invoking this connector (403 otherwise).
"""

import base64
import logging
import shlex
import uuid
import warnings
from pathlib import Path
from typing import Any, Protocol

from modulo.connectors.base import (
    ConnectorBase,
    ConnectorPayload,
    ConnectorPermissionError,
    ConnectorQuery,
    ConnectorResult,
    ConnectorType,
    HealthResult,
)

_log = logging.getLogger(__name__)

# Raised/reported when no runtime provider is bound to the connector (S1192).
_ERR_RUNTIME_NOT_CONFIGURED = "Runtime provider not configured"


class ShellRuntimeProvider(Protocol):
    """Legacy workspace operations consumed by ShellConnector."""

    async def execute_command(
        self,
        workspace: Any,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_seconds: int = 60,
    ) -> dict[str, Any]: ...


class ShellConnector(ConnectorBase):
    """Execute shell commands and manage files inside a workspace lease.

    .. deprecated::
        ShellConnector is deprecated since ADR 003 (2026-07-16).  The Modulo
        agent execution environment model (ADR 001) has been superseded by the
        Agent Dispatch Model (ADR 003).  Modulo no longer runs agents inside
        sandboxes — it dispatches work to external agent runtimes via the
        ``sandbox_agent`` node type.  ShellConnector will be removed in a
        future release.

    Requires an active ``workspace_lease_id`` — 403 if not set.
    Command allowlist is enforced via ``allowed_commands``.

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
        runtime_provider: ShellRuntimeProvider | None = None,
        allowed_commands: list[str] | None = None,
        runtime_provider_hub: Any | None = None,
        environment_profile_id: uuid.UUID | None = None,
        workspace_lease_id: uuid.UUID | None = None,
    ) -> None:
        warnings.warn(
            "ShellConnector is deprecated (ADR 003). Use sandbox_agent node type instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        self._runtime_provider = runtime_provider
        self._allowed_commands = frozenset(allowed_commands or [])
        self._runtime_provider_hub = runtime_provider_hub
        self._environment_profile_id = environment_profile_id
        self._workspace_lease_id = workspace_lease_id

    @property
    def connector_type(self) -> ConnectorType:
        return ConnectorType.SHELL

    def write_reported_failure(self, result: Any) -> bool:
        """FAR-531 (AC6): shell write results carry the executed command's
        ``exit_code`` (both the ``command`` and ``file`` resources return it). A
        non-zero exit code is a REPORTED failure without a raise — the write did
        not reach upstream, so the idempotency stamp must not treat the
        non-raising return as a delivery.

        QA Fix 4: an ABSENT-or-``None`` distinction matters. ``exit_code: None``
        (E2B CAN produce it — ``CommandsExecResult.exit_code`` is Optional for
        killed / failed-to-start commands, and the runtime provider only
        defaults on a MISSING attribute) is treated as a reported failure: a
        killed command is NOT a confirmed delivery, so the result must not
        stamp ``delivery_done`` and suppress the re-run in every mode. A result
        WITHOUT an ``exit_code`` key at all is not a REPORTED failure (nothing
        to report) — the pre-FAR-531 delivery semantics apply."""
        return isinstance(result, dict) and "exit_code" in result and result.get("exit_code") != 0

    async def health_check(self) -> HealthResult:
        if self._runtime_provider is None and self._runtime_provider_hub is None:
            return HealthResult(ok=False, detail=_ERR_RUNTIME_NOT_CONFIGURED)
        return HealthResult(ok=True, detail="ShellConnector ready")

    def _check_workspace_lease(self) -> None:
        if self._workspace_lease_id is None and self._runtime_provider is None:
            raise ConnectorPermissionError(
                "No active workspace lease and no runtime provider configured.",
            )

    async def _resolve_profile_from_hub(self) -> Any | None:
        if self._environment_profile_id is None:
            return None
        try:
            from modulo.db.crud.environment_profile import get_environment_profile
            from modulo.db.session import AsyncSessionLocal

            async with AsyncSessionLocal() as session:
                return await get_environment_profile(session, self._environment_profile_id)
        except Exception:
            _log.exception("Failed to resolve environment profile from hub")
            return None

    def _check_command_allowed(self, command: list[str]) -> None:
        if not self._allowed_commands:
            raise ConnectorPermissionError(
                "No commands are allowed (deny-all). "
                "Operator must configure permitted commands in the environment profile.",
            )
        base = command[0] if command else ""
        if base not in self._allowed_commands:
            raise ConnectorPermissionError(
                f"Command {base!r} is not in the allowed list: {sorted(self._allowed_commands)}",
            )

    async def query(self, q: ConnectorQuery) -> ConnectorResult:
        if (
            self._runtime_provider is None
            and self._runtime_provider_hub is not None
            and self._environment_profile_id is not None
        ):
            profile = await self._resolve_profile_from_hub()
            if profile is not None:
                provider = self._runtime_provider_hub.resolve(profile)
                if provider is not None:
                    self._runtime_provider = provider

        if self._runtime_provider is None:
            raise ValueError(_ERR_RUNTIME_NOT_CONFIGURED)
        self._check_workspace_lease()
        provider_ref: str | None = q.filters.get("provider_ref")

        match q.resource:
            case "file":
                path = q.filters["path"]
                safe_path = shlex.quote(path)
                result = await self._runtime_provider.execute_command(
                    provider_ref,
                    f"cat {safe_path}",
                    timeout_seconds=30,
                )
                if result["exit_code"] != 0:
                    raise ValueError(f"Failed to read file {path!r}: {(result['stderr'] or '').strip()}")
                return ConnectorResult(records=[{"path": path, "content": result["stdout"]}])

            case "directory":
                dir_path = q.filters.get("path", ".")
                safe_path = shlex.quote(dir_path)
                result = await self._runtime_provider.execute_command(
                    provider_ref,
                    f"ls -1a {safe_path}",
                    timeout_seconds=30,
                )
                entries: list[dict[str, Any]] = []
                for line in (result["stdout"] or "").strip().split("\n"):
                    name = line.strip()
                    if name and name not in (".", ".."):
                        resolved = f"{dir_path.rstrip('/')}/{name}"
                        entries.append({"name": name, "path": resolved})
                return ConnectorResult(records=entries, total=len(entries))

            case _:
                raise ValueError(f"Unsupported shell query resource: {q.resource!r}")

    async def write(self, payload: ConnectorPayload) -> dict[str, Any]:
        if (
            self._runtime_provider is None
            and self._runtime_provider_hub is not None
            and self._environment_profile_id is not None
        ):
            profile = await self._resolve_profile_from_hub()
            if profile is not None:
                provider = self._runtime_provider_hub.resolve(profile)
                if provider is not None:
                    self._runtime_provider = provider

        if self._runtime_provider is None:
            raise ValueError(_ERR_RUNTIME_NOT_CONFIGURED)
        self._check_workspace_lease()
        provider_ref: str | None = payload.data.get("provider_ref")

        match payload.resource:
            case "command":
                command_str: str = payload.data["command"]
                command_parts = shlex.split(command_str)
                self._check_command_allowed(command_parts)

                env: dict[str, str] | None = payload.data.get("env")
                timeout: int = payload.data.get("timeout_seconds", 60)
                cwd: str | None = payload.data.get("cwd")

                cmd = self._build_exec_cmd(command_str, cwd, env)
                exec_result = await self._runtime_provider.execute_command(
                    provider_ref,
                    cmd,
                    timeout_seconds=timeout,
                )
                return {
                    "stdout": exec_result["stdout"],
                    "stderr": exec_result["stderr"],
                    "exit_code": exec_result["exit_code"],
                    "duration_ms": exec_result.get("duration_ms", 0),
                    "masked": True,
                }

            case "file":
                path: str = payload.data["path"]
                content: str = payload.data["content"]
                safe_path = shlex.quote(path)
                encoded = base64.b64encode(content.encode()).decode()
                parent = str(Path(path).parent)
                safe_parent = shlex.quote(parent)

                exec_result = await self._runtime_provider.execute_command(
                    provider_ref,
                    f"mkdir -p {safe_parent} && echo '{encoded}' | base64 -d > {safe_path}",
                    timeout_seconds=30,
                )
                if exec_result["exit_code"] != 0:
                    raise ValueError(f"Failed to write file {path!r}: {(exec_result['stderr'] or '').strip()}")
                return {
                    "path": path,
                    "bytes_written": len(content),
                    "exit_code": exec_result["exit_code"],
                }

            case _:
                raise ValueError(f"Unsupported shell write resource: {payload.resource!r}")

    def _build_exec_cmd(
        self,
        command_str: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> str:
        command_parts = shlex.split(command_str)
        if not cwd and not env:
            return command_str

        shell_parts: list[str] = []
        if cwd:
            shell_parts.append(f"cd {shlex.quote(cwd)}")
        quoted_cmd = " ".join(shlex.quote(p) for p in command_parts)
        if env:
            env_prefix = " ".join(f"{shlex.quote(k)}={shlex.quote(v)}" for k, v in env.items())
            quoted_cmd = f"{env_prefix} {quoted_cmd}"
        shell_parts.append(quoted_cmd)
        return " && ".join(shell_parts)
