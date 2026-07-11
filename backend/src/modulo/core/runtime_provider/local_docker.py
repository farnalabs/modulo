from __future__ import annotations

"""Local Docker RuntimeProvider — ephemeral containers via the docker Python SDK."""


import io
import logging
import os
import tarfile
import time
import uuid
from typing import Any

import docker
from docker.errors import DockerException, ImageNotFound, NotFound

from modulo.core.runtime_provider.base import RuntimeProvider

_log = logging.getLogger(__name__)

_DEFAULT_IMAGE = "python:3.12-slim"
_DEFAULT_MEMORY_MB = 512
_DEFAULT_CPU = 1
_WORKSPACE_PREFIX = "modulo-ws-"
_UUID_TRUNC_LEN = 12


class LocalDockerRuntimeProvider(RuntimeProvider):
    """RuntimeProvider backed by ephemeral Docker containers via the docker SDK.

    Each workspace is a Docker container created from the profile's image_ref.
    Containers are kept alive via ``sleep infinity`` and auto-removed when stopped.

    The Docker daemon URL is resolved in this order:
    1. ``docker_host`` constructor argument
    2. ``DOCKER_HOST`` environment variable
    3. ``None`` (local socket — default)
    """

    def __init__(
        self,
        docker_host: str | None = None,
        default_image: str = _DEFAULT_IMAGE,
        timeout_seconds: int = 120,
    ) -> None:
        self._docker_host = docker_host or os.environ.get("DOCKER_HOST")
        self._default_image = default_image
        self._timeout_seconds = timeout_seconds
        self._client: docker.DockerClient | None = None
        self._workspaces: dict[str, str] = {}

    @property
    def provider_id(self) -> str:
        return "local_docker"

    def _get_client(self) -> docker.DockerClient:
        if self._client is None:
            try:
                self._client = (
                    docker.from_env() if not self._docker_host else docker.DockerClient(base_url=self._docker_host)
                )
            except DockerException as exc:
                raise RuntimeError(f"Failed to connect to Docker daemon: {exc}") from exc
        return self._client

    async def create_workspace(self, profile: Any, session: Any = None) -> Any:
        """Create a Docker container as the workspace.

        Returns a dict with container_id and name for use as the workspace object.
        """
        image = getattr(profile, "image_ref", None) or self._default_image
        ref = uuid.uuid4().hex[:_UUID_TRUNC_LEN]
        container_name = f"{_WORKSPACE_PREFIX}{ref}"

        config_json = getattr(profile, "config_json", None) or {}
        memory_mb = config_json.get("memory_mb", _DEFAULT_MEMORY_MB)
        cpu_count = config_json.get("cpu", _DEFAULT_CPU)

        try:
            client = self._get_client()
            await self._ensure_image(client, image)
            container = client.containers.create(
                image=image,
                command=["sleep", "infinity"],
                name=container_name,
                detach=True,
                mem_limit=f"{memory_mb}m",
                nano_cpus=int(cpu_count * 1e9),
                auto_remove=True,
            )
            container.start()
        except DockerException as exc:
            raise RuntimeError(f"Failed to create container for workspace {ref}: {exc}") from exc

        workspace = {"container_id": container.id, "name": container_name, "ref": ref}
        self._workspaces[ref] = container.id
        return workspace

    async def _ensure_image(self, client: docker.DockerClient, image: str) -> None:
        """Pull the image if not already present locally."""
        try:
            client.images.get(image)
        except ImageNotFound:
            _log.info("Pulling image %s ...", image)
            try:
                client.images.pull(image)
            except DockerException as exc:
                raise RuntimeError(f"Failed to pull image {image}: {exc}") from exc

    async def destroy_workspace(self, workspace: Any) -> None:
        container_id = self._resolve_container_id(workspace)
        if container_id is None:
            return
        try:
            client = self._get_client()
            container = client.containers.get(container_id)
            container.stop(timeout=5)
        except NotFound:
            pass
        except DockerException:
            _log.warning("Failed to destroy container %s", container_id, exc_info=True)

    async def workspace_health(self, workspace: Any) -> bool:
        container_id = self._resolve_container_id(workspace)
        if container_id is None:
            return False
        try:
            client = self._get_client()
            container = client.containers.get(container_id)
            return container.status == "running"
        except (NotFound, DockerException):
            return False

    async def execute_command(
        self,
        workspace: Any,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_seconds: int = 60,
    ) -> dict[str, Any]:
        container_id = self._resolve_container_id(workspace)
        if container_id is None:
            raise ValueError(f"Unknown workspace: {workspace}")
        try:
            client = self._get_client()
            container = client.containers.get(container_id)
            exec_env = None
            if env:
                exec_env = {k: str(v) for k, v in env.items()}
            exec_cmd = ["sh", "-c", command]
            if cwd:
                exec_cmd = ["sh", "-c", f"cd {cwd} && {command}"]
            exit_code, output = container.exec_run(
                cmd=exec_cmd,
                environment=exec_env,
                workdir=cwd,
                demux=True,
            )
            stdout_bytes, stderr_bytes = output
            return {
                "stdout": (stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else ""),
                "stderr": (stderr_bytes.decode("utf-8", errors="replace") if stderr_bytes else ""),
                "exit_code": exit_code,
            }
        except (NotFound, DockerException) as exc:
            raise RuntimeError(f"Command execution failed in container {container_id}: {exc}") from exc

    async def write_file(self, workspace: Any, path: str, content: str) -> None:
        container_id = self._resolve_container_id(workspace)
        if container_id is None:
            raise ValueError(f"Unknown workspace: {workspace}")
        try:
            client = self._get_client()
            container = client.containers.get(container_id)
            tar_buffer = io.BytesIO()
            with tarfile.open(fileobj=tar_buffer, mode="w") as tar:
                data = content.encode("utf-8")
                info = tarfile.TarInfo(name=path.lstrip("/"))
                info.size = len(data)
                info.mtime = int(time.time())
                tar.addfile(info, io.BytesIO(data))
            tar_buffer.seek(0)
            container.put_archive("/", tar_buffer)
        except (NotFound, DockerException) as exc:
            raise RuntimeError(f"Failed to write file {path} in container {container_id}: {exc}") from exc

    async def read_file(self, workspace: Any, path: str) -> str:
        container_id = self._resolve_container_id(workspace)
        if container_id is None:
            raise ValueError(f"Unknown workspace: {workspace}")
        try:
            client = self._get_client()
            container = client.containers.get(container_id)
            tar_stream, _ = container.get_archive(path)
            tar_buffer = io.BytesIO()
            for chunk in tar_stream:
                tar_buffer.write(chunk)
            tar_buffer.seek(0)
            with tarfile.open(fileobj=tar_buffer, mode="r") as tar:
                member = tar.next()
                if member is None:
                    raise RuntimeError(f"Empty archive reading {path}")
                content = tar.extractfile(member)
                if content is None:
                    raise RuntimeError(f"Failed to extract {path}")
                return content.read().decode("utf-8")
        except (NotFound, DockerException) as exc:
            raise RuntimeError(f"Failed to read file {path} from container {container_id}: {exc}") from exc

    async def list_files(self, workspace: Any, path: str) -> list[str]:
        result = await self.execute_command(workspace, f"ls -1a {path}", timeout_seconds=30)
        if result["exit_code"] != 0:
            raise RuntimeError(f"Failed to list files at {path}: {result['stderr']}")
        lines = result["stdout"].strip().split("\n")
        return [line.strip() for line in lines if line.strip() and line.strip() not in (".", "..")]

    def _resolve_container_id(self, workspace: Any) -> str | None:
        if isinstance(workspace, dict):
            ref = workspace.get("ref")
            if ref and ref in self._workspaces:
                return self._workspaces[ref]
            cid = workspace.get("container_id")
            if cid:
                return cid
            return None
        if isinstance(workspace, str):
            return self._workspaces.get(workspace)
        return None

    async def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
