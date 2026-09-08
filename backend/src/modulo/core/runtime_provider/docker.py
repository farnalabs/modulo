"""Docker RuntimeProvider — ephemeral containers via aiodocker."""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import socket
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

import aiodocker

from modulo.core.runtime_provider import ExecProcess, ExecResult, ExecStreamChunk, RuntimeProvider, WorkspaceSpec

_log = logging.getLogger(__name__)

_DEFAULT_IMAGE = "python:3.13-slim"
# D4 hardening default (ADR 029 config table): 1.0 CPU / 1 GiB per workspace.
_DEFAULT_MEMORY_MB = 1024
_WORKSPACE_PREFIX = "modulo-workspace-"
_UUID_TRUNC_LEN = 12
_CLOSE_DESTROY_TIMEOUT_S = 30

# --- Workspace hardening defaults (FAR-590 D4 / ADR 029 config table) ------
# Per-workspace resources: 1.0 CPU / 1 GiB (fixed per ADR 029's committed
# table; per-profile overrides are a separately ticketed future effort).
_DEFAULT_HARDENING_CPU = 1.0
_DEFAULT_HARDENING_MEMORY_MB = 1024
# Read-only rootfs + tmpfs workdir. The bundled runner image's agent HOME
# (config/session/cache dirs) and /tmp are tmpfs-backed with sizing adequate
# for an opencode session (verified by the GA opencode scenario).
_TMPFS_WORKDIR = "/home/user"
_TMPFS_WORKDIR_SIZE = "size=512m,mode=1777"
_TMPFS_TMP = "/tmp"  # noqa: S108 # nosec B108  # NOSONAR - container-side tmpfs mount path (mode=1777 sticky-bit, dropped caps)
_TMPFS_TMP_SIZE = "size=128m,mode=1777"
# Dropped capabilities + no-new-privileges (no cap_add is granted).
_CAP_DROP = ["ALL"]
_SECURITY_OPT = ["no-new-privileges:true"]
# The bundled runner image runs as the non-root `runner` uid (1001).
_RUNNER_USER = "1001:1001"
_IMAGES_WITH_RUNNER_USER = ("modulo-runner",)
# Dedicated workspace bridge network default (compose overlay-declared).
_DEFAULT_WORKSPACE_NETWORK = "modulo-runner-workspace"
# Deployment-identity label (reconciler machine scoping, ADR 029): sourced
# from this env var with a hostname fallback — two Modulo deployments sharing
# one engine must not destroy each other's workspaces.
_DEPLOYMENT_IDENTITY_ENV = "MODULO_RUNNER_MACHINE_ID"
_DEPLOYMENT_IDENTITY_LABEL = "modulo.machine.id"


def _route_by_channel(channel: Any, data: Any) -> tuple[bytes, bytes]:
    """Route a (channel, data) pair: channel 1 -> stdout, anything else -> stderr."""
    if channel == 1:
        return (bytes(data or b""), b"")
    return (b"", bytes(data or b""))


def _split_two_element_frame(frame: Any) -> tuple[bytes, bytes]:
    """Split a 2-element tuple/list exec frame into (stdout, stderr) byte payloads."""
    first, second = frame
    if isinstance(first, int) and not isinstance(first, bool):
        # aiodocker Message / (fileno, data) shape.
        return _route_by_channel(first, second)
    # Test-double shape (stdout_bytes, stderr_bytes).
    return (first or b"", second or b"")


def _message_frame_channel_and_data(frame: Any) -> tuple[Any, Any]:
    """Extract ``(channel, data)`` from an aiodocker ``Message``-shaped frame.

    Older shapes carried ``channel``; aiodocker 0.27 uses ``stream``.
    """
    channel: Any = getattr(frame, "stream", None)
    if channel is None:
        channel = getattr(frame, "channel", None)
    data: Any = getattr(frame, "data", None)
    if data is None and isinstance(frame, (bytes, bytearray)):
        data = bytes(frame)
    return channel, data


def _split_exec_frame(frame: Any) -> tuple[bytes, bytes]:
    """Split one Docker exec stream frame into (stdout, stderr) byte payloads.

    Handles both frame shapes:
    - aiodocker's ``Message`` (a NamedTuple of ``(stream: int, data: bytes)``
      — 1 = stdout, 2 = stderr); a 2-tuple whose first element is an int.
    - the test-double shape ``(stdout_bytes, stderr_bytes)`` — a 2-tuple of
      byte-ish values (or None).
    """
    if isinstance(frame, (tuple, list)) and len(frame) == 2:
        return _split_two_element_frame(frame)
    channel, data = _message_frame_channel_and_data(frame)
    # Unknown channel/shape is treated as stderr so diagnostic output is
    # never silently dropped.
    return _route_by_channel(channel, data)


async def _open_exec_stream(exec_instance: Any) -> Any:
    """Start an exec in hijacked-stream mode and return its output stream.

    aiodocker 0.27's ``Exec.start(detach=False)`` is a plain method returning
    a :class:`Stream` (NOT awaitable); older aiodockers and AsyncMock test
    doubles return an awaitable. Accept both shapes — awaiting a Stream
    raises ``TypeError: object Stream can't be used in 'await' expression``
    against a real engine.
    """
    started = exec_instance.start(detach=False)
    if inspect.isawaitable(started):
        return await started
    return started


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

    provider_id = "runner_docker"
    provider_aliases = frozenset({"docker", "local_docker"})

    def __init__(
        self,
        docker_host: str | None = None,
        default_image: str = _DEFAULT_IMAGE,
        create_timeout: int = 120,
        start_timeout: int = 30,
        workspace_network: str = _DEFAULT_WORKSPACE_NETWORK,
    ) -> None:
        self._docker_host = docker_host or os.environ.get("MODULO_DOCKER_HOST") or os.environ.get("DOCKER_HOST")
        self._default_image = default_image
        self._create_timeout = create_timeout
        self._start_timeout = start_timeout
        self._workspace_network = workspace_network
        self._client: aiodocker.Docker | None = None
        self._client_lock = asyncio.Lock()
        self._workspaces: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Hub integration
    # ------------------------------------------------------------------

    def supports(self, profile: Any) -> bool:
        """Best-effort auto-detection kept for the deprecated local_docker adapter.

        Deterministic hub resolution (FAR-587) no longer consults supports();
        the deprecated LocalDockerRuntimeProvider adapter still delegates here.
        """
        hint = getattr(profile, "provider_hint", None) or ""
        if hint.lower() == "docker":
            return True
        image_ref = getattr(profile, "image_ref", None) or ""
        return "docker" in image_ref.lower()

    # ------------------------------------------------------------------
    # RuntimeProvider interface
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_memory_mb(raw_memory: Any) -> int:
        """Parse and clamp the spec's ``memory_mb`` limit (D4: 4 MiB floor, 128 GiB ceiling)."""
        try:
            memory_mb = int(raw_memory)
        except (ValueError, TypeError):
            memory_mb = _DEFAULT_MEMORY_MB
        return max(4, min(memory_mb, 131072))

    @staticmethod
    def _build_container_env(labels: dict[str, str] | None) -> list[str]:
        """Map ``spec.labels`` to container Env entries (env-var injection).

        Docker is the ONLY provider consuming spec.labels (FAR-595 contract):
        E2B/Local ignore it — clone inputs ride the first-class
        spec.repo_url / spec.repo_ref fields, which this provider does not
        act on (the bundled runner image handles code sync).
        """
        env = []
        for k, v in (labels or {}).items():
            entry = f"{k}={v}"
            if any(c in entry for c in ("\n", "\r", "\0")):
                _log.warning("Skipping env entry with control characters: %s", k)
            else:
                env.append(entry)
        return env

    def _build_workspace_labels(self, spec: WorkspaceSpec) -> dict[str, str]:
        """Map provider-neutral workspace metadata to container Labels.

        (deployment-identity / org / run correlation, ADR 029). This is
        separate from ``spec.labels`` (Env injection) and from
        ``repo_url``/``repo_ref`` (clone semantics, unused here).
        """
        workspace_labels = dict(spec.workspace_metadata or {})
        # Deployment-identity label: machine-scoped reconciler filters ride
        # on it (two deployments sharing one engine never destroy each
        # other's workspaces). The creation marker drives reconciler ages.
        workspace_labels.setdefault(_DEPLOYMENT_IDENTITY_LABEL, self._deployment_identity())
        workspace_labels.setdefault("modulo.created_at", str(int(time.time())))
        return workspace_labels

    def _resolve_network_mode(self, spec: WorkspaceSpec) -> str:
        """Resolve the container network mode from the spec's egress policy.

        ``none`` is opt-in per profile; the default (outbound permitted —
        the tier's purpose) attaches the dedicated workspace bridge, never
        the compose/backend network that hosts the Docker endpoint.
        """
        if (spec.egress_policy or "").strip().lower() == "none":
            return "none"
        return spec.workspace_network or self._workspace_network

    @staticmethod
    def _build_container_config(
        image: str,
        memory_mb: int,
        env: list[str],
        network_mode: str,
        workspace_labels: dict[str, str],
    ) -> dict[str, Any]:
        """Build the container create config (D4 hardening defaults, ADR 029)."""
        host_config: dict[str, Any] = {
            "AutoRemove": True,
            "Memory": memory_mb * 1024 * 1024,
            # D4 hardening: 1.0 CPU default, read-only rootfs, tmpfs
            # workdir/tmp, dropped caps, no-new-privileges.
            "NanoCpus": int(_DEFAULT_HARDENING_CPU * 1_000_000_000),
            "ReadonlyRootfs": True,
            "Tmpfs": {
                _TMPFS_WORKDIR: _TMPFS_WORKDIR_SIZE,
                _TMPFS_TMP: _TMPFS_TMP_SIZE,
            },
            "CapDrop": list(_CAP_DROP),
            "SecurityOpt": list(_SECURITY_OPT),
            "NetworkMode": network_mode,
        }
        config: dict[str, Any] = {
            "Image": image,
            "Cmd": ["sleep", "infinity"],
            "Env": env,
            "Labels": workspace_labels,
            "HostConfig": host_config,
        }
        # Non-root user at provision. Only stamped on first-party runner
        # images (generic base images carry no runner user).
        if any(marker in image.lower() for marker in _IMAGES_WITH_RUNNER_USER):
            config["User"] = _RUNNER_USER
        return config

    async def _pull_image_best_effort(self, client: aiodocker.Docker, image: str) -> None:
        """Best-effort provision pull: ensure the image exists before create.

        POST /images/create is in the allowlist; a pull failure surfaces on
        container create for unreachable refs.
        """
        try:
            pull = client.images.pull(image)
            if asyncio.iscoroutine(pull):
                await pull
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.info("workspace image pull skipped/failed (best-effort): %s", image, exc_info=True)

    async def _create_and_start_container(
        self,
        client: aiodocker.Docker,
        config: dict[str, Any],
        container_name: str,
    ) -> Any:
        """Create the workspace container and start it (bounded waits)."""
        container = await asyncio.wait_for(
            client.containers.create(
                config=config,
                name=container_name,
            ),
            timeout=self._create_timeout,
        )
        await asyncio.wait_for(container.start(), timeout=self._start_timeout)
        return container

    async def create_workspace(self, spec: WorkspaceSpec) -> str:
        """Create a hardened Docker container as the workspace.

        The container runs ``sleep infinity`` so it stays alive for
        subsequent ``exec_command`` calls. Auto-removal is enabled.

        Hardening defaults (FAR-590 D4 / ADR 029 — applied at provision):
        non-root user (runner uid 1001 on modulo-runner images), read-only
        rootfs + tmpfs workdir/tmp with adequate sizing, dropped caps +
        no-new-privileges, 1.0 CPU / 1 GiB resources, dedicated workspace
        bridge network (``none`` opt-in per profile), structured labels from
        ``spec.workspace_metadata`` + the machine deployment-identity label.
        """
        client = await self._get_client()
        image = spec.image_ref.strip() if spec.image_ref else self._default_image
        ref = uuid.uuid4().hex[:_UUID_TRUNC_LEN]
        memory_mb = self._resolve_memory_mb(spec.resource_limits.get("memory_mb", _DEFAULT_MEMORY_MB))
        container_name = f"{_WORKSPACE_PREFIX}{ref}"

        env = self._build_container_env(spec.labels)
        workspace_labels = self._build_workspace_labels(spec)
        network_mode = self._resolve_network_mode(spec)
        config = self._build_container_config(image, memory_mb, env, network_mode, workspace_labels)

        try:
            await self._pull_image_best_effort(client, image)
            container = await self._create_and_start_container(client, config, container_name)
        except asyncio.CancelledError:
            raise
        except OSError as exc:
            _log.exception("Failed to reach Docker daemon for workspace %s", ref)
            raise RuntimeError(f"Unable to reach the Docker daemon (is it running?): {exc}") from exc
        except Exception:
            _log.exception("Failed to create container for workspace %s", ref)
            raise

        self._workspaces[ref] = container.id
        return ref

    @staticmethod
    def _deployment_identity() -> str:
        """Machine deployment identity for container labels (reconciler scoping)."""
        return os.environ.get(_DEPLOYMENT_IDENTITY_ENV) or socket.gethostname()

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
        stream: Any = await _open_exec_stream(exec_instance)
        stdout_chunks: list[bytes] = []
        stderr_chunks: list[bytes] = []
        while True:
            frame = await stream.read_out()
            if frame is None:
                break
            out, err = _split_exec_frame(frame)
            if out:
                stdout_chunks.append(out)
            if err:
                stderr_chunks.append(err)
        info = await exec_instance.inspect()
        raw_exit = info.get("ExitCode")
        exit_code = int(raw_exit) if raw_exit is not None else -1
        return b"".join(stdout_chunks), b"".join(stderr_chunks), exit_code

    async def exec_command_stream(
        self,
        provider_ref: str,
        command: list[str],
        *,
        environment: dict[str, str] | None = None,
    ) -> ExecProcess:
        """Stream exec output as async chunks with a kill handle (D4 primitive).

        Docker implementation of the ABC streaming primitive: an exec created
        + started in hijacked-stream mode (phase-0 spike-verified through the
        filtered socket proxy), emitting decoded :class:`ExecStreamChunk`
        values. When the stream ends naturally the exec is inspected and
        ``process.exit_code`` is populated. An engine/proxy drop mid-stream
        surfaces as a stream ERROR (never a fabricated silent end with a
        zero exit code) — the dispatch layer routes that to a retryable
        failure.
        """
        container_id = self._get_container_id(provider_ref)
        client = await self._get_client()
        container = await client.containers.get(container_id)
        exec_instance: Any = await container.exec(cmd=command, environment=environment or None)
        stream: Any = await _open_exec_stream(exec_instance)

        process = ExecProcess(chunks=None, kill=None)  # type: ignore[arg-type]

        async def _chunks() -> AsyncIterator[ExecStreamChunk]:
            exit_code: int | None = None
            try:
                while True:
                    try:
                        frame = await stream.read_out()
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        # Engine/proxy death mid-exec: a stream ERROR, never
                        # a silent end with a fabricated success code.
                        process.error = f"{type(exc).__name__}: {str(exc)[:200]}"
                        return
                    if frame is None:
                        break
                    out, err = _split_exec_frame(frame)
                    if out:
                        yield ExecStreamChunk(stream="stdout", data=out.decode("utf-8", errors="replace"))
                    if err:
                        yield ExecStreamChunk(stream="stderr", data=err.decode("utf-8", errors="replace"))
                try:
                    info = await exec_instance.inspect()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    # The engine/proxy can also die between the stream end and
                    # the inspect (e.g. an engine kill right at completion) —
                    # the same stream-ERROR contract applies.
                    process.error = f"{type(exc).__name__}: {str(exc)[:200]}"
                    return
                raw_exit = info.get("ExitCode")
                # A container/engine kill mid-exec can report ExitCode=None —
                # the exec has NO inspectable exit code, which the dispatch
                # layer classifies as retryable (never a fabricated success).
                exit_code = int(raw_exit) if raw_exit is not None else None
            finally:
                process.exit_code = exit_code
                process.done.set()

        process.chunks = _chunks()

        async def _kill() -> None:
            try:
                # aiodocker 0.27's Stream.close() is sync; older shapes may
                # return an awaitable — accept both.
                closed = stream.close()
                if inspect.isawaitable(closed):
                    await closed
            except asyncio.CancelledError:
                raise
            except Exception:
                _log.info("workspace exec stream close failed (best-effort)", exc_info=True)

        process._kill = _kill
        return process

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
        """Close the underlying Docker client connection and clean up workspaces.

        Each destroy is bounded by :meth:`asyncio.wait_for` (30s per workspace)
        so a hung Docker daemon cannot stall teardown forever; on timeout the
        workspace reference is force-dropped and teardown continues.
        """
        for provider_ref in list(self._workspaces.keys()):
            try:
                await asyncio.wait_for(
                    self.destroy_workspace(provider_ref),
                    timeout=_CLOSE_DESTROY_TIMEOUT_S,
                )
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                # destroy_workspace already popped the reference before its
                # first await; a timeout mid-teardown means the id is now
                # untracked (by design) - log and continue.
                _log.warning(
                    "Timed out destroying workspace %s during close(); force-dropping it",
                    provider_ref,
                )
            except Exception:
                _log.exception("Failed to destroy workspace %s during close()", provider_ref)
        if self._client is not None:
            try:
                await asyncio.wait_for(self._client.close(), timeout=_CLOSE_DESTROY_TIMEOUT_S)
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                _log.warning("Timed out closing Docker client during close(); force-dropping it")
                self._client = None
            except Exception:
                _log.exception("Failed to close Docker client during close()")
                self._client = None
            else:
                self._client = None
