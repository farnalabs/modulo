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

    provider_id = "local_docker"
    provider_aliases = frozenset({"docker"})

    def __init__(
        self,
        docker_host: str | None = None,
        default_image: str = _DEFAULT_IMAGE,
        create_timeout: int = 120,
        start_timeout: int = 30,
    ) -> None:
        self._docker_host = docker_host or os.environ.get("MODULO_DOCKER_HOST") or os.environ.get("DOCKER_HOST")
        self._default_image = default_image
        self._create_timeout = create_timeout
        self._start_timeout = start_timeout
        self._client: aiodocker.Docker | None = None
        self._client_lock = asyncio.Lock()
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
        raw_memory = spec.resource_limits.get("memory_mb", _DEFAULT_MEMORY_MB)
        try:
            memory_mb = int(raw_memory)
        except (ValueError, TypeError):
            memory_mb = _DEFAULT_MEMORY_MB
        memory_mb = max(4, min(memory_mb, 131072))
        container_name = f"{_WORKSPACE_PREFIX}{ref}"

        env = []
        for k, v in (spec.labels or {}).items():
            entry = f"{k}={v}"
            if any(c in entry for c in ("\n", "\r", "\0")):
                _log.warning("Skipping env entry with control characters: %s", k)
            else:
                env.append(entry)

        try:
            container = await asyncio.wait_for(
                client.containers.create(
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
                ),
                timeout=self._create_timeout,
            )
            await asyncio.wait_for(container.start(), timeout=self._start_timeout)
        except asyncio.CancelledError:
            raise
        except (ConnectionError, OSError) as exc:
            _log.exception("Failed to reach Docker daemon for workspace %s", ref)
            raise RuntimeError(f"Unable to reach the Docker daemon (is it running?): {exc}") from exc
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
        cmd_timeout: int | None = None,
    ) -> ExecResult:
        """Run a command inside the workspace container."""
        container_id = self._get_container_id(provider_ref)
        client = await self._get_client()
        container = await client.containers.get(container_id)
        exec_instance = await container.exec(cmd=command)

        start = time.monotonic()
        try:
            stdout_bytes, stderr_bytes, exit_code = await self._run_exec_with_timeout(exec_instance, cmd_timeout)
        except TimeoutError:
            duration = int((time.monotonic() - start) * 1000)
            _log.warning("exec_command timed out for container %s", container_id)
            return ExecResult(
                exit_code=-1,
                stdout="",
                stderr="Command timed out",
                duration_ms=duration,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("exec_command failed for container %s", container_id)
            raise

        duration = int((time.monotonic() - start) * 1000)
        return ExecResult(
            exit_code=exit_code,
            stdout=stdout_bytes.decode("utf-8", errors="replace"),
            stderr=stderr_bytes.decode("utf-8", errors="replace"),
            duration_ms=duration,
        )

    async def _run_exec_with_timeout(
        self,
        exec_instance: Any,
        cmd_timeout: int | None,
    ) -> tuple[bytes, bytes, int]:
        """Collect exec output, applying an optional asyncio timeout."""
        if cmd_timeout is not None:
            return await asyncio.wait_for(self._collect_exec_output(exec_instance), timeout=cmd_timeout)
        return await self._collect_exec_output(exec_instance)

    async def _collect_exec_output(self, exec_instance: Any) -> tuple[bytes, bytes, int]:
        """Stream stdout/stderr from an exec instance and return decoded output."""
        stream: Any = await exec_instance.start(detach=False)
        stdout_chunks: list[bytes] = []
        stderr_chunks: list[bytes] = []
        while True:
            frame = await stream.read_out()
            if frame is None:
                break
            out, err = frame
            if out:
                stdout_chunks.append(out)
            if err:
                stderr_chunks.append(err)
        info = await exec_instance.inspect()
        exit_code = info.get("ExitCode", -1)
        return b"".join(stdout_chunks), b"".join(stderr_chunks), exit_code

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
        except asyncio.CancelledError:
            raise
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
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("Failed to get status for container %s", container_id)
            return "unknown"

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _get_client(self) -> aiodocker.Docker:
        if self._client is None:
            async with self._client_lock:
                if self._client is None:
                    self._client = aiodocker.Docker(url=self._docker_host)
        return self._client

    def _get_container_id(self, provider_ref: str) -> str:
        container_id = self._workspaces.get(provider_ref)
        if container_id is None:
            raise ValueError(f"Unknown workspace: {provider_ref}")
        return container_id

    async def close(self) -> None:
        """Close the underlying Docker client connection and clean up workspaces."""
        for provider_ref in list(self._workspaces.keys()):
            await self.destroy_workspace(provider_ref)
        if self._client is not None:
            await self._client.close()
            self._client = None
