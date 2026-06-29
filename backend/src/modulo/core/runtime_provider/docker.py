"""Docker RuntimeProvider — ephemeral containers via aiodocker.

Usage:
    provider = DockerRuntimeProvider()
    ref = await provider.create_workspace(spec)
    result = await provider.exec_command(ref, ["python3", "--version"])
    await provider.destroy_workspace(ref)
"""

from __future__ import annotations

import logging
import os
import uuid
from typing import Any

import aiodocker

from modulo.core.runtime_provider import ExecResult, RuntimeProvider, WorkspaceSpec

_log = logging.getLogger(__name__)


class DockerRuntimeProvider(RuntimeProvider):
    """RuntimeProvider backed by ephemeral Docker containers.

    Each workspace is a Docker container created from the spec's ``image_ref``.
    Containers are kept alive via ``sleep infinity`` and auto-removed when
    stopped.

    The Docker daemon URL is resolved in this order:
    1. ``docker_host`` constructor argument
    2. ``MODULO_DOCKER_HOST`` environment variable
    3. ``DOCKER_HOST`` environment variable
    4. ``None`` (local socket — default)
    """

    def __init__(
        self,
        docker_host: str | None = None,
        default_image: str = "python:3.13-slim",
    ) -> None:
        self._docker_host = (
            docker_host
            or os.environ.get("MODULO_DOCKER_HOST")
            or os.environ.get("DOCKER_HOST")
        )
        self._default_image = default_image
        self._client: aiodocker.Docker | None = None
        self._workspaces: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Hub integration
    # ------------------------------------------------------------------

    def supports(self, profile: Any) -> bool:
        hint = getattr(profile, "provider_hint", None) or ""
        if hint.lower() == "docker":
            return True
        image_ref = getattr(profile, "image_ref", None) or ""
        return "docker" in image_ref.lower()

    # ------------------------------------------------------------------
    # RuntimeProvider interface
    # ------------------------------------------------------------------

    async def create_workspace(self, spec: WorkspaceSpec) -> str:
        """Create a Docker container as the workspace.

        The container runs ``sleep infinity`` so it stays alive for
        subsequent ``exec_command`` calls. Auto-removal is enabled.
        """
        client = await self._get_client()
        image = spec.image_ref.strip() if spec.image_ref else self._default_image
        ref = uuid.uuid4().hex[:12]
        memory_mb = spec.resource_limits.get("memory_mb", 512)
        container_name = f"modulo-workspace-{ref}"

        container = await client.containers.create(
            config={
                "Image": image,
                "Cmd": ["sleep", "infinity"],
                "Env": [f"{k}={v}" for k, v in (spec.labels or {}).items()],
                "HostConfig": {
                    "AutoRemove": True,
                    "Memory": memory_mb * 1024 * 1024,
                },
            },
            name=container_name,
        )
        await container.start()

        self._workspaces[ref] = container.id
        return ref

    async def exec_command(
        self,
        provider_ref: str,
        command: list[str],
        *,
        timeout: int | None = None,  # noqa: ASYNC109
    ) -> ExecResult:
        """Run a command inside the workspace container.

        *timeout* is accepted for ABC compatibility; the underlying Docker
        exec API does not natively support a timeout — use ``HostConfig``
        or orchestration-level timeouts for enforcement.
        """
        container_id = self._get_container_id(provider_ref)
        client = await self._get_client()
        container = await client.containers.get(container_id)
        exec_instance = await container.exec(cmd=command)
        raw_output = await exec_instance.start()
        return ExecResult(
            exit_code=0,
            stdout=raw_output.decode("utf-8", errors="replace"),
            stderr="",
        )

    async def destroy_workspace(self, provider_ref: str) -> None:
        """Stop and remove the workspace container.

        Best-effort: if the container is already gone (e.g. due to
        ``AutoRemove``) the error is logged and swallowed.
        """
        container_id = self._workspaces.pop(provider_ref, None)
        if container_id is None:
            return
        try:
            client = await self._get_client()
            container = await client.containers.get(container_id)
            await container.stop()
            await container.delete()
        except Exception:
            _log.exception("Failed to destroy container %s", container_id)

    async def get_workspace_status(self, provider_ref: str) -> str:
        """Return the current container status."""
        container_id = self._workspaces.get(provider_ref)
        if container_id is None:
            return "terminated"
        try:
            client = await self._get_client()
            container = await client.containers.get(container_id)
            info = await container.show()
            return info.get("State", {}).get("Status", "unknown")
        except Exception:
            return "terminated"

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _get_client(self) -> aiodocker.Docker:
        if self._client is None:
            self._client = aiodocker.Docker(url=self._docker_host)
        return self._client

    def _get_container_id(self, provider_ref: str) -> str:
        container_id = self._workspaces.get(provider_ref)
        if container_id is None:
            raise ValueError(f"Unknown workspace: {provider_ref}")
        return container_id

    async def close(self) -> None:
        """Close the underlying Docker client connection."""
        if self._client is not None:
            await self._client.close()
            self._client = None


def create_docker_provider_from_env() -> DockerRuntimeProvider:
    """Build a DockerRuntimeProvider configured from environment variables.

    Reads ``MODULO_DOCKER_HOST`` (falls back to ``DOCKER_HOST``) for the
    daemon URL.
    """
    return DockerRuntimeProvider()
