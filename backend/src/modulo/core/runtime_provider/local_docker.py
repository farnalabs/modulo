"""Legacy Local Docker API backed by the canonical async Docker provider."""

from __future__ import annotations

import base64
import shlex
import uuid
from typing import Any

from modulo.core.runtime_provider import WorkspaceSpec
from modulo.core.runtime_provider.docker import DockerRuntimeProvider


class LocalDockerRuntimeProvider:
    """Compatibility adapter for callers using the original Docker contract.

    New orchestration code should use :class:`DockerRuntimeProvider` directly.
    This adapter preserves the public Local Docker constructor and dictionary
    command results without maintaining a second Docker implementation.
    """

    provider_id = "local_docker"
    provider_aliases = frozenset({"docker"})

    def __init__(
        self,
        docker_host: str | None = None,
        default_image: str = "python:3.12-slim",
        timeout_seconds: int = 120,
        *,
        provider: DockerRuntimeProvider | None = None,
    ) -> None:
        self._provider = provider or DockerRuntimeProvider(
            docker_host=docker_host,
            default_image=default_image,
            create_timeout=timeout_seconds,
        )

    async def create_workspace(self, profile: WorkspaceSpec | Any, session: Any = None) -> dict[str, str]:
        """Create a workspace and return the original mapping-shaped handle."""
        del session
        spec = profile if isinstance(profile, WorkspaceSpec) else self._spec_from_profile(profile)
        ref = await self._provider.create_workspace(spec)
        return {"ref": ref, "container_id": self._provider._get_container_id(ref)}

    async def destroy_workspace(self, workspace: Any) -> None:
        await self._provider.destroy_workspace(self._resolve_ref(workspace))

    async def workspace_health(self, workspace: Any) -> bool:
        status = await self._provider.get_workspace_status(self._resolve_ref(workspace))
        return status == "running"

    async def execute_command(
        self,
        workspace: Any,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_seconds: int = 60,
    ) -> dict[str, Any]:
        """Execute a shell command and return the original mapping result."""
        shell_parts: list[str] = []
        if cwd:
            shell_parts.append(f"cd {shlex.quote(cwd)}")
        if env:
            assignments = " ".join(f"{shlex.quote(key)}={shlex.quote(str(value))}" for key, value in env.items())
            command = f"env {assignments} {command}"
        shell_parts.append(command)
        result = await self._provider.exec_command(
            self._resolve_ref(workspace),
            ["sh", "-c", " && ".join(shell_parts)],
            timeout=timeout_seconds,
        )
        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.exit_code,
            "duration_ms": result.duration_ms,
        }

    async def write_file(self, workspace: Any, path: str, content: str) -> None:
        encoded = base64.b64encode(content.encode()).decode()
        safe_path = shlex.quote(path)
        safe_parent = shlex.quote(str(path.rpartition("/")[0] or "."))
        result = await self.execute_command(
            workspace,
            f"mkdir -p {safe_parent} && printf %s {shlex.quote(encoded)} | base64 -d > {safe_path}",
            timeout_seconds=30,
        )
        if result["exit_code"] != 0:
            raise RuntimeError(f"Failed to write file {path}: {result['stderr']}")

    async def read_file(self, workspace: Any, path: str) -> str:
        result = await self.execute_command(
            workspace,
            f"cat {shlex.quote(path)}",
            timeout_seconds=30,
        )
        if result["exit_code"] != 0:
            raise RuntimeError(f"Failed to read file {path}: {result['stderr']}")
        stdout = result["stdout"]
        return stdout if isinstance(stdout, str) else str(stdout)

    async def list_files(self, workspace: Any, path: str) -> list[str]:
        result = await self.execute_command(
            workspace,
            f"ls -1a {shlex.quote(path)}",
            timeout_seconds=30,
        )
        if result["exit_code"] != 0:
            raise RuntimeError(f"Failed to list files at {path}: {result['stderr']}")
        stdout = result["stdout"]
        lines = stdout.splitlines() if isinstance(stdout, str) else []
        return [line.strip() for line in lines if line.strip() not in {"", ".", ".."}]

    def supports(self, profile: Any) -> bool:
        return self._provider.supports(profile)

    async def close(self) -> None:
        await self._provider.close()

    @staticmethod
    def _resolve_ref(workspace: Any) -> str:
        if isinstance(workspace, str) and workspace:
            return workspace
        if isinstance(workspace, dict):
            ref = workspace.get("ref")
            if isinstance(ref, str) and ref:
                return ref
        raise ValueError(f"Unknown workspace: {workspace}")

    @staticmethod
    def _spec_from_profile(profile: Any) -> WorkspaceSpec:
        profile_id = getattr(profile, "id", None)
        organisation_id = getattr(profile, "organisation_id", None)
        if not isinstance(profile_id, uuid.UUID) or not isinstance(organisation_id, uuid.UUID):
            raise TypeError("profile must provide UUID id and organisation_id fields")

        capabilities = getattr(profile, "capabilities_json", [])
        config = getattr(profile, "config_json", {})
        return WorkspaceSpec(
            environment_profile_id=profile_id,
            organisation_id=organisation_id,
            image_ref=str(getattr(profile, "image_ref", "") or ""),
            capabilities=[item for item in capabilities if isinstance(item, str)]
            if isinstance(capabilities, list)
            else [],
            resource_limits=dict(config) if isinstance(config, dict) else {},
        )


__all__ = ["LocalDockerRuntimeProvider"]
