"""Docker RuntimeProvider — ephemeral containers via aiodocker."""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from typing import Any

import aiodocker

from modulo.core.runtime_provider import ExecResult, RuntimeProvider, WorkspaceSpec

_log = logging.getLogger(__name__)

_DEFAULT_IMAGE = "python:3.13-slim"
_DEFAULT_MEMORY_MB = 512
_WORKSPACE_PREFIX = "modulo-workspace-"
_UUID_TRUNC_LEN = 12


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
        default_image: str = _DEFAULT_IMAGE,
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
        ref = uuid.uuid4().hex[:_UUID_TRUNC_LEN]
        memory_mb = spec.resource_limits.get("memory_mb", _DEFAULT_MEMORY_MB)
        if not isinstance(memory_mb, int) or memory_mb < 4:
            memory_mb = _DEFAULT_MEMORY_MB
        container_name = f"{_WORKSPACE_PREFIX}{ref}"

        env = []
        for k, v in (spec.labels or {}).items():
            entry = f"{k}={v}"
            if any(c in entry for c in ("\n", "\r", "\0")):
                _log.warning("Skipping env entry with control characters: %s", k)
            else:
                env.append(entry)

        try:
            container = await client.containers.create(
                config={
                    "Image": image,
                    "Cmd": ["sleep", "infinity"],
                    "Env": env,
                    "HostConfig": {
                        "AutoRemove": True,
                        "Memory": memory_mb * 1024 * 1024,
                    },
                },
                name=container_name,
            )
            await container.start()
        except Exception:
            _log.exception("Failed to create container for workspace %s", ref)
            raise

        self._workspaces[ref] = container.id
        return ref

    async def exec_command(
        self,
        provider_ref: str,
        command: list[str],
        *,
        timeout: int | None = None,  # noqa: ASYNC109
    ) -> ExecResult:
        """Run a command inside the workspace container."""
        container_id = self._get_container_id(provider_ref)
        client = await self._get_client()
        container = await client.containers.get(container_id)
        exec_instance = await container.exec(cmd=command)

        async def _run_exec() -> tuple[bytes, bytes, int]:
            stream: Any = await exec_instance.start(detach=False)  # type: ignore[misc]
            stdout_bytes, stderr_bytes = await stream.read_out()
            info = await exec_instance.inspect()
            exit_code = info.get("ExitCode", -1)
            return stdout_bytes or b"", stderr_bytes or b"", exit_code

        start = time.monotonic()
        try:
            if timeout is not None:
                stdout_bytes, stderr_bytes, exit_code = await asyncio.wait_for(
                    _run_exec(), timeout=timeout
                )
            else:
                stdout_bytes, stderr_bytes, exit_code = await _run_exec()
            duration = int((time.monotonic() - start) * 1000)
        except TimeoutError:
            duration = int((time.monotonic() - start) * 1000)
            _log.warning("exec_command timed out for container %s", container_id)
            return ExecResult(
                exit_code=-1,
                stdout="",
                stderr="Command timed out",
                duration_ms=duration,
            )
        except Exception:
            _log.exception("exec_command failed for container %s", container_id)
            raise

        return ExecResult(
            exit_code=exit_code,
            stdout=stdout_bytes.decode("utf-8", errors="replace"),
            stderr=stderr_bytes.decode("utf-8", errors="replace"),
            duration_ms=duration,
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
        except aiodocker.exceptions.DockerError:
            _log.warning("Container %s already removed", container_id)
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
            status: str = info.get("State", {}).get("Status", "unknown")
            return status
        except aiodocker.exceptions.DockerError:
            return "terminated"
        except Exception:
            _log.exception("Failed to get status for container %s", container_id)
            return "unknown"

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
