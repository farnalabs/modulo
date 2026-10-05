"""Kubernetes RuntimeProvider — long-lived workspace pods (FAR-1051).

Shape (settled by four rounds of adversarial review; ADR 040 contract frozen):
ONE long-lived Pod per workspace, kept alive by a POSIX-sh wait loop;
``exec_command`` and ``exec_command_stream`` map onto the Kubernetes exec
subresource (``pods/exec`` WebSocket, ``v4.channel.k8s.io``) exactly as the
Docker provider maps onto the engine exec API; the ``RuntimeProvider`` ABC is
implemented UNCHANGED. The only contract delta this delivery ships is
registering the ``kubernetes`` provider type (vocabulary + env-gated hub
registration + CHECK-widening migration).

Security posture (the reason this tier exists)
-----------------------------------------------
- The control plane holds a NAMESPACED ServiceAccount credential scoped to
  the workspace namespace — never the host-root-equivalent Docker Engine
  API socket. The workspace pod itself does NOT automount an API token
  (``automount_service_account_token=False``): workspaces never need the
  Kubernetes API.
- The workspace namespace should enforce the Pod Security Admission
  ``restricted`` standard. Every pod this provider builds targets that
  profile explicitly (non-root uid 1001, ``allowPrivilegeEscalation=false``,
  ``readOnlyRootFilesystem``, all capabilities dropped, RuntimeDefault
  seccomp). A customer admission policy can reject any spec we build —
  infrastructure we cannot bypass even if our code is wrong.
- Egress is deliberately NOT ours. Bounded egress is the CUSTOMER's
  NetworkPolicy on the workspace namespace (opt-in; requires a CNI that
  enforces NetworkPolicy). There is deliberately NO bespoke egress gateway
  in this module: an ``egress_policy`` of ``none`` at provision time, or a
  selected-mode allowlist / ``none`` request at isolation time, is refused
  with the typed :class:`ProviderCapabilityUnsupportedError` naming
  NetworkPolicy as the remediation — never a silent downgrade.

Auth and config
---------------
In-cluster ServiceAccount configuration when ``KUBERNETES_SERVICE_HOST`` is
set (the pod's own mounted token), otherwise the standard kubeconfig chain
(``KUBECONFIG`` / default path). Client configuration is loaded lazily on
first use, so registering the provider at boot never fails on a machine
without a kubeconfig. Namespace: ``MODULO_KUBERNETES_NAMESPACE`` (default
``modulo``). Workspace-pod ServiceAccount:
``MODULO_KUBERNETES_SERVICE_ACCOUNT`` (default ``default``).

WorkspaceSpec mapping
---------------------
- ``image_ref`` -> container image (default ``python:3.13-slim``, Docker
  parity); the image must provide a POSIX ``sh`` (the exec-based
  file-I/O defaults already assume one).
- ``resource_limits['memory_mb']`` -> memory request+limit (clamped 4 MiB ..
  128 GiB); CPU is fixed at 1 core (Docker parity — ``cpu_usage_pct`` in
  ``resource_limits`` is the platform-side kill threshold, not a cgroup
  limit).
- ``labels`` -> container env vars (env-var injection, FAR-595; entries with
  control characters are skipped with a log, Docker parity — an invalid env
  NAME surfaces as a loud HTTP 422 from the API, never a silent skip).
- ``workspace_metadata`` -> pod annotations (raw, always fits) + sanitised
  pod labels (63-char valid label values) for future label-selector sweeps.
- ``repo_url`` / ``repo_ref`` -> ignored (the bundled runner image handles
  code sync; Docker parity).
- ``allow_root_user`` -> skips the non-root uid stamp, logged as a warning.
- ``timeout_seconds`` -> bounds the provision wait (pod reaching Running);
  falls back to 120s when unset/zero. Direct callers are themselves
  wrapped by the dispatch provisioning watchdog.
- ``egress_policy`` -> ``none`` refused (see security posture).

Deliberately deferred (FAR-1051's "not frozen" list): pod-lifetime deadline
scoping (no ``active_deadline_seconds`` — the default 3600s spec timeout
must not kill a long-lived workspace), egress default posture, and
dispatcher-minted secret naming. The orphan sweep IS wired:
:meth:`KubernetesRuntimeProvider.list_workspace_pods` feeds the
provider-neutral ``runner_reconciler`` sweep, which stays the single owner
of reclamation for every tier.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shlex
import socket
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from aiohttp import WSMsgType
from kubernetes_asyncio import client as k8s_client
from kubernetes_asyncio import config as k8s_config
from kubernetes_asyncio.client import (
    V1Capabilities,
    V1Container,
    V1EmptyDirVolumeSource,
    V1EnvVar,
    V1ObjectMeta,
    V1Pod,
    V1PodSecurityContext,
    V1PodSpec,
    V1ResourceRequirements,
    V1SeccompProfile,
    V1SecurityContext,
    V1Volume,
    V1VolumeMount,
)
from kubernetes_asyncio.client.exceptions import ApiException
from kubernetes_asyncio.stream import WsApiClient
from kubernetes_asyncio.stream.ws_client import ERROR_CHANNEL, STDERR_CHANNEL, STDOUT_CHANNEL

from modulo.core.runtime_provider import (
    BackendUnreachableError,
    ExecProcess,
    ExecResult,
    ExecStreamChunk,
    IsolationPolicy,
    ProviderCapabilityUnsupportedError,
    ProvisionTimeoutError,
    RuntimeProvider,
    RuntimeProviderError,
    UnknownRefError,
    WorkspaceSpec,
)

_log = logging.getLogger(__name__)

_DEFAULT_IMAGE = "python:3.13-slim"
_DEFAULT_MEMORY_MB = 1024
_DEFAULT_CPU = "1"
_WORKSPACE_POD_PREFIX = "modulo-ws-"
_UUID_TRUNC_LEN = 16
_CONTAINER_NAME = "workspace"
_DEFAULT_NAMESPACE = "modulo"
_DEFAULT_SERVICE_ACCOUNT = "default"
_NAMESPACE_ENV = "MODULO_KUBERNETES_NAMESPACE"
_SERVICE_ACCOUNT_ENV = "MODULO_KUBERNETES_SERVICE_ACCOUNT"
_DEPLOYMENT_IDENTITY_ENV = "MODULO_RUNNER_MACHINE_ID"
_DEFAULT_PROVISION_TIMEOUT_S = 120
_PROVISION_POLL_INTERVAL = 1.0
_LOG_TAIL_LINES = 5000
_CLOSE_DESTROY_TIMEOUT_S = 30
_STREAM_ERROR_TRUNC = 200

# Workspace hardening defaults (mirror the Docker provider's D4 posture).
_RUNNER_UID = 1001
_WORKSPACE_MOUNT = "/home/user"
_WORKSPACE_EMPTY_DIR_SIZE = "512Mi"
_TMP_MOUNT = "/tmp"  # noqa: S108 # nosec B108 # NOSONAR - container-side volume mount path (mode 0777, dropped caps)
_TMP_EMPTY_DIR_SIZE = "128Mi"
_POD_GRACE_PERIOD_SECONDS = 5
# POSIX-sh wait loop: portable across GNU and busybox ``sleep`` (neither
# ``sleep infinity`` nor a fixed large value is portable across both).
_WORKSPACE_WAIT_CMD = ["sh", "-c", "while :; do sleep 3600; done"]

# Pod identity labels/annotations (reconciliation + attribution).
_PROVIDER_LABEL = "modulo.provider"
_PROVIDER_LABEL_VALUE = "kubernetes"
_CREATED_AT_LABEL = "modulo.created_at"
_MACHINE_ANNOTATION = "modulo.machine.id"

# Pod label keys/values are a restricted vocabulary (RFC 1123 label syntax);
# raw metadata always rides the annotations where anything fits.
_LABEL_VALUE_INVALID_RE = re.compile(r"[^A-Za-z0-9_.-]")
_LABEL_KEY_RE = re.compile(r"^([A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?)(/[A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?)?$")
_LABEL_VALUE_MAX = 63
# POSIX identifier — what a ``export NAME=...`` shell injection needs.
_ENV_NAME_RE = re.compile(r"[A-Za-z_]\w*", re.ASCII)


def _stream_error_message(exc: BaseException) -> str:
    """Format an API/proxy stream failure for ``ExecProcess.error`` (D4)."""
    return f"{type(exc).__name__}: {str(exc)[:_STREAM_ERROR_TRUNC]}"


def _resolve_exit_code(payload: str | None) -> tuple[int | None, str | None]:
    """Resolve the exec exit status from the exec-subresource error-channel payload.

    Returns ``(exit_code, None)`` on a parseable status (including a real
    non-zero exit — a command failure is a result, not a stream error) and
    ``(None, note)`` when the stream ended with no status or an unparseable
    one. Never fabricates ``0``.
    """
    if payload is None:
        return None, "exec stream ended without an exit-status message"
    try:
        return WsApiClient.parse_error_data(payload), None
    except (ValueError, TypeError, KeyError):
        return None, f"unparseable exec exit status: {payload[:_STREAM_ERROR_TRUNC]}"


@dataclass
class _ExecStreamState:
    """Shared state one exec stream's reader records for its consumer."""

    exit_payload: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class WorkspacePodRef:
    """One Modulo workspace pod as the reconciler's sweep sees it (FAR-1051).

    Deliberately carries the pod's labels RAW rather than a pre-extracted run
    id: the ``modulo.run.id`` label key belongs to the workspace-orphan
    reconciler (it owns the label vocabulary it filters on for every
    provider), while pod identity — namespace, deployment-identity
    annotation, creation timestamp — belongs to this provider.

    ``created_age_s`` is ``now - modulo.created_at`` (epoch seconds), or
    ``0.0`` when the marker is absent/unparseable — never a negative or
    fabricated age, so a pod without a creation marker can never become a
    grace-period destroy candidate.
    """

    ref: str
    labels: dict[str, str]
    created_age_s: float


class KubernetesRuntimeProvider(RuntimeProvider):
    """RuntimeProvider backed by long-lived Kubernetes pods.

    See the module docstring for the security posture, the WorkspaceSpec
    mapping, and the deliberately-deferred decisions.
    """

    provider_id = "kubernetes"
    provider_aliases = frozenset({"k8s"})

    def __init__(
        self,
        namespace: str | None = None,
        default_image: str = _DEFAULT_IMAGE,
        provision_timeout_s: int = _DEFAULT_PROVISION_TIMEOUT_S,
        kubeconfig: str | None = None,
    ) -> None:
        raw_namespace = namespace or os.environ.get(_NAMESPACE_ENV) or _DEFAULT_NAMESPACE
        self._namespace = raw_namespace.strip() or _DEFAULT_NAMESPACE
        self._service_account = (os.environ.get(_SERVICE_ACCOUNT_ENV) or _DEFAULT_SERVICE_ACCOUNT).strip()
        self._default_image = default_image
        self._provision_timeout_s = provision_timeout_s
        self._kubeconfig = kubeconfig
        self._configuration: Any = None
        self._api_client: Any = None
        self._ws_api_client: Any = None
        self._core_api: Any = None
        self._ws_core_api: Any = None
        self._client_lock = asyncio.Lock()
        self._workspaces: set[str] = set()

    # ------------------------------------------------------------------
    # Client bootstrap (lazy — registration at boot never touches the network)
    # ------------------------------------------------------------------

    async def _get_configuration(self) -> Any:
        """Load the Kubernetes client configuration (in-cluster, else kubeconfig).

        Takes the client lock itself; callers must NOT hold it (asyncio.Lock
        is not re-entrant — _get_core/_get_ws_core load the configuration
        BEFORE taking the lock for their own client construction).
        """
        if self._configuration is not None:
            return self._configuration
        async with self._client_lock:
            if self._configuration is None:
                configuration = k8s_client.Configuration()
                try:
                    if os.environ.get("KUBERNETES_SERVICE_HOST"):
                        # Load-bearing ignore: kubernetes_asyncio ships no
                        # annotations for load_incluster_config (unlike
                        # load_kube_config); required by CI's exact mypy
                        # invocation — never remove without upstream stubs.
                        k8s_config.load_incluster_config(client_configuration=configuration)  # type: ignore[no-untyped-call]
                    else:
                        await k8s_config.load_kube_config(
                            config_file=self._kubeconfig,
                            client_configuration=configuration,
                        )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    raise BackendUnreachableError(
                        "Kubernetes configuration could not be loaded (no in-cluster service "
                        f"account and no usable kubeconfig): {type(exc).__name__}: {exc}"
                    ) from exc
                self._configuration = configuration
            return self._configuration

    async def _get_core(self) -> Any:
        """Return the plain CoreV1Api (Pod CRUD, logs)."""
        if self._core_api is not None:
            return self._core_api
        configuration = await self._get_configuration()
        async with self._client_lock:
            if self._core_api is None:
                self._api_client = k8s_client.ApiClient(configuration=configuration)
                self._core_api = k8s_client.CoreV1Api(api_client=self._api_client)
            return self._core_api

    async def _get_ws_core(self) -> Any:
        """Return the WebSocket-backed CoreV1Api (exec subresource only)."""
        if self._ws_core_api is not None:
            return self._ws_core_api
        configuration = await self._get_configuration()
        async with self._client_lock:
            if self._ws_core_api is None:
                self._ws_api_client = WsApiClient(configuration=configuration)
                self._ws_core_api = k8s_client.CoreV1Api(api_client=self._ws_api_client)
            return self._ws_core_api

    # ------------------------------------------------------------------
    # Workspace spec -> pod mapping helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_memory_mb(raw_memory: Any) -> int:
        """Parse and clamp the spec's ``memory_mb`` limit (4 MiB floor, 128 GiB ceiling)."""
        try:
            memory_mb = int(raw_memory)
        except (ValueError, TypeError):
            memory_mb = _DEFAULT_MEMORY_MB
        return max(4, min(memory_mb, 131072))

    @staticmethod
    def _deployment_identity() -> str:
        """Machine deployment identity for pod annotations (reconciler scoping).

        ``MODULO_RUNNER_MACHINE_ID`` first, hostname fallback — Docker parity
        (``DockerRuntimeProvider._deployment_identity``). The constant
        fallback this had (``"unspecified"``) collapsed two deployments
        sharing one namespace onto ONE identity, so each reconciler would
        sweep the other's pods as same-machine; a hostname (unique per
        machine) keeps them distinct even when the operator never set the
        env var.
        """
        return os.environ.get(_DEPLOYMENT_IDENTITY_ENV) or socket.gethostname()

    @staticmethod
    def _sanitize_label_value(raw: str) -> str:
        """Coerce *raw* into a valid <=63-char RFC 1123 label value (deterministically)."""
        value = _LABEL_VALUE_INVALID_RE.sub("-", raw)[:_LABEL_VALUE_MAX]
        value = value.strip("-._")
        return value or "v"

    def _build_metadata(self, spec: WorkspaceSpec) -> tuple[dict[str, str], dict[str, str]]:
        """Map workspace metadata to pod (labels, annotations).

        Annotations carry the RAW metadata (anything fits, never lossy);
        labels carry a sanitised copy of the same entries plus the identity
        keys a label-selector sweep filters on, so an arbitrary metadata
        value can never produce an invalid pod spec.

        Reserved identity keys are stamped AFTER the metadata loop (ordering
        mirror of ``DockerRuntimeProvider._build_workspace_labels``) and win
        outright: operator-supplied ``workspace_metadata`` carrying
        ``modulo.provider`` / ``modulo.created_at`` / ``modulo.machine.id``
        must never re-stamp them. A pod whose ``modulo.provider`` label said
        anything but ``kubernetes`` would be refused by
        ``destroy_workspace_by_ref`` and filtered out by
        ``list_workspace_pods`` — a permanent, unreclaimable leak. Identity
        keys are ASSIGNED (not ``setdefault``): the identity is this
        provider's own fact, never caller input. Non-reserved keys (e.g.
        ``modulo.run.id``, stamped by dispatch) still flow through.
        """
        metadata = dict(spec.workspace_metadata or {})

        labels: dict[str, str] = {}
        for key, value in metadata.items():
            if _LABEL_KEY_RE.match(key) and len(key) <= _LABEL_VALUE_MAX:
                labels[key] = self._sanitize_label_value(str(value))
        # Reserved identity labels — applied last, always authoritative.
        labels[_PROVIDER_LABEL] = _PROVIDER_LABEL_VALUE
        labels[_CREATED_AT_LABEL] = str(int(time.time()))

        annotations = dict(metadata)
        # Reserved identity annotations — likewise applied last.
        annotations[_PROVIDER_LABEL] = _PROVIDER_LABEL_VALUE
        annotations[_MACHINE_ANNOTATION] = self._deployment_identity()
        return labels, annotations

    @staticmethod
    def _build_container_env(labels: dict[str, str] | None) -> list[V1EnvVar]:
        """Map ``spec.labels`` to container env vars (env-var injection, FAR-595).

        Entries carrying control characters are skipped with a log (Docker
        parity); an invalid env NAME is deliberately NOT pre-filtered — the
        API rejects it loudly (HTTP 422) instead of silently dropping the
        injection.
        """
        env: list[V1EnvVar] = []
        for key, value in (labels or {}).items():
            entry = f"{key}={value}"
            if any(char in entry for char in ("\n", "\r", "\0")):
                _log.warning("Skipping env entry with control characters: %s", key)
            else:
                env.append(V1EnvVar(name=key, value=value))
        return env

    def _build_pod(
        self,
        spec: WorkspaceSpec,
        pod_name: str,
        image: str,
        memory_mb: int,
        labels: dict[str, str],
        annotations: dict[str, str],
    ) -> V1Pod:
        """Assemble the hardened workspace pod spec (Pod Security ``restricted``)."""
        # Pod Security "restricted" stamps: uid 1001 + non-root unless the
        # profile explicitly opts in to root (Docker parity, FAR-1036). The
        # root branch deliberately passes NO run_as_* fields (the image's own
        # USER applies, as on Docker); fsGroup lives on the POD security
        # context and stamps the workspace/tmp emptyDirs for uid 1001 writes.
        non_root_kwargs: dict[str, Any] = {}
        if spec.allow_root_user:
            _log.warning(
                "workspace image %s running as root (allow_root_user=True) — this weakens the "
                "security boundary and may be rejected by a restricted Pod Security namespace",
                image,
            )
        else:
            non_root_kwargs = {"run_as_non_root": True, "run_as_user": _RUNNER_UID, "run_as_group": _RUNNER_UID}
        container_security = V1SecurityContext(
            allow_privilege_escalation=False,
            privileged=False,
            capabilities=V1Capabilities(drop=["ALL"]),
            read_only_root_filesystem=True,
            seccomp_profile=V1SeccompProfile(type="RuntimeDefault"),
            **non_root_kwargs,
        )
        pod_security = None if spec.allow_root_user else V1PodSecurityContext(fs_group=_RUNNER_UID)

        container = V1Container(
            name=_CONTAINER_NAME,
            image=image,
            command=list(_WORKSPACE_WAIT_CMD),
            env=self._build_container_env(spec.labels),
            resources=V1ResourceRequirements(
                requests={"cpu": _DEFAULT_CPU, "memory": f"{memory_mb}Mi"},
                limits={"cpu": _DEFAULT_CPU, "memory": f"{memory_mb}Mi"},
            ),
            security_context=container_security,
            volume_mounts=[
                V1VolumeMount(name="workspace", mount_path=_WORKSPACE_MOUNT),
                V1VolumeMount(name="tmp", mount_path=_TMP_MOUNT),
            ],
        )
        pod_spec = V1PodSpec(
            restart_policy="Never",
            containers=[container],
            volumes=[
                V1Volume(
                    name="workspace",
                    empty_dir=V1EmptyDirVolumeSource(medium="Memory", size_limit=_WORKSPACE_EMPTY_DIR_SIZE),
                ),
                V1Volume(
                    name="tmp",
                    empty_dir=V1EmptyDirVolumeSource(medium="Memory", size_limit=_TMP_EMPTY_DIR_SIZE),
                ),
            ],
            service_account_name=self._service_account or None,
            # Workspaces never need the Kubernetes API — no mounted token.
            automount_service_account_token=False,
            termination_grace_period_seconds=_POD_GRACE_PERIOD_SECONDS,
            security_context=pod_security,
        )
        return V1Pod(
            metadata=V1ObjectMeta(name=pod_name, labels=labels, annotations=annotations),
            spec=pod_spec,
        )

    # ------------------------------------------------------------------
    # RuntimeProvider interface
    # ------------------------------------------------------------------

    async def create_workspace(self, spec: WorkspaceSpec) -> str:
        """Create a hardened workspace pod and wait until it is Running.

        Returns the pod name (the provider ref — ref-only destroy works after
        a process restart by construction). Provision failure reclaims the pod
        best-effort before re-raising, so a failed create never leaks one.
        """
        egress = (spec.egress_policy or "").strip().lower()
        if egress == "none":
            raise ProviderCapabilityUnsupportedError(
                "The Kubernetes tier cannot enforce an egress 'none' workspace: pod networking is "
                "owned by the cluster CNI. Express deny-all as a customer NetworkPolicy on the "
                "workspace namespace (FAR-1051) and use a profile this tier can enforce."
            )

        core = await self._get_core()
        image = spec.image_ref.strip() if spec.image_ref else self._default_image
        pod_name = f"{_WORKSPACE_POD_PREFIX}{uuid.uuid4().hex[:_UUID_TRUNC_LEN]}"
        memory_mb = self._resolve_memory_mb(spec.resource_limits.get("memory_mb", _DEFAULT_MEMORY_MB))
        labels, annotations = self._build_metadata(spec)
        pod = self._build_pod(spec, pod_name, image, memory_mb, labels, annotations)
        wait_bound_s = float(spec.timeout_seconds or self._provision_timeout_s)

        try:
            await core.create_namespaced_pod(namespace=self._namespace, body=pod)
        except asyncio.CancelledError:
            raise
        except ApiException as exc:
            _log.exception("Kubernetes API rejected workspace pod %s", pod_name)
            raise RuntimeProviderError(
                f"Kubernetes API rejected workspace pod create (HTTP {exc.status}): {exc.reason}"
            ) from exc
        except Exception as exc:
            raise BackendUnreachableError(
                f"Kubernetes API unreachable while creating workspace pod {pod_name}: {type(exc).__name__}: {exc}"
            ) from exc

        try:
            await self._wait_until_running(pod_name, wait_bound_s)
        except BaseException:
            # Provision failed or was cancelled — reclaim the pod so a failed
            # create never leaks it (the shipped reconciler sweep is
            # Docker-only; this is the only cleanup on this path).
            await self._delete_pod_best_effort(pod_name)
            raise

        self._workspaces.add(pod_name)
        return pod_name

    async def _wait_until_running(self, pod_name: str, wait_bound_s: float) -> None:
        """Poll the pod until its phase is ``Running`` (bounded)."""
        deadline = time.monotonic() + wait_bound_s
        phase = "unknown"
        note = ""
        while True:
            pod = await self._read_pod(pod_name)
            if pod is None:
                raise RuntimeProviderError(f"Kubernetes workspace pod {pod_name} disappeared while provisioning")
            phase, note = self._pod_phase_and_note(pod)
            if phase == "running":
                return
            if phase in ("failed", "succeeded"):
                raise RuntimeProviderError(
                    f"Kubernetes workspace pod {pod_name} entered phase {phase} before running"
                    + (f" ({note})" if note else "")
                )
            if time.monotonic() >= deadline:
                raise ProvisionTimeoutError(
                    f"Kubernetes workspace pod {pod_name} did not reach Running within "
                    f"{wait_bound_s:.0f}s (phase {phase}" + (f": {note})" if note else ")")
                )
            await asyncio.sleep(_PROVISION_POLL_INTERVAL)

    @staticmethod
    def _pod_phase_and_note(pod: Any) -> tuple[str, str]:
        """Return ``(lowercase phase, diagnostic note)`` from a V1Pod status."""
        status = getattr(pod, "status", None)
        phase = str(getattr(status, "phase", "") or "").strip().lower()
        waiting_note = ""
        terminated_note = ""
        for container_status in getattr(status, "container_statuses", None) or []:
            state = getattr(container_status, "state", None)
            waiting = getattr(state, "waiting", None)
            if waiting is not None and not waiting_note:
                reason = str(getattr(waiting, "reason", "") or "")
                message = str(getattr(waiting, "message", "") or "")
                if reason:
                    waiting_note = f"{reason}: {message}" if message else reason
            terminated = getattr(state, "terminated", None)
            if terminated is not None and not terminated_note:
                reason = str(getattr(terminated, "reason", "") or "")
                exit_code = getattr(terminated, "exit_code", None)
                if reason or exit_code is not None:
                    terminated_note = f"{reason} (exit {exit_code})" if reason else f"exit {exit_code}"
        return phase, waiting_note or terminated_note

    async def _read_pod(self, pod_name: str) -> Any | None:
        """Read the workspace pod; ``None`` when it is gone (404)."""
        core = await self._get_core()
        try:
            return await core.read_namespaced_pod(name=pod_name, namespace=self._namespace)
        except asyncio.CancelledError:
            raise
        except ApiException as exc:
            if exc.status == 404:
                return None
            raise RuntimeProviderError(
                f"Kubernetes API error reading pod {pod_name} (HTTP {exc.status}): {exc.reason}"
            ) from exc
        except Exception as exc:
            raise BackendUnreachableError(
                f"Kubernetes API unreachable while reading pod {pod_name}: {type(exc).__name__}: {exc}"
            ) from exc

    async def _delete_pod_best_effort(self, pod_name: str) -> None:
        """Delete the pod, swallowing (with a log) every failure but cancellation."""
        try:
            core = await self._get_core()
            await core.delete_namespaced_pod(name=pod_name, namespace=self._namespace)
        except asyncio.CancelledError:
            raise
        except ApiException as exc:
            if exc.status != 404:
                _log.info("best-effort pod delete failed for %s (HTTP %s)", pod_name, exc.status)
        except Exception:
            _log.info("best-effort pod delete failed for %s", pod_name, exc_info=True)

    async def exec_command(
        self,
        provider_ref: str,
        command: list[str],
        *,
        cmd_timeout: int | None = None,
    ) -> ExecResult:
        """Run a command inside the workspace pod (collect-then-return).

        A healthy stream end resolves the exit status from the exec
        subresource's error-channel payload (a real non-zero exit is a
        result); a timeout returns ``exit_code=-1`` (Docker parity); an
        API/proxy drop mid-stream raises the typed
        :class:`BackendUnreachableError` — the command outcome is unknown and
        must never be reported as a command failure or a success.
        """
        ws = await self._open_exec(provider_ref, command)
        state = _ExecStreamState()
        start = time.monotonic()
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []

        async def _collect() -> None:
            async for stream_name, data in self._read_exec_frames(ws, state):
                if stream_name == "stdout":
                    stdout_parts.append(data)
                else:
                    stderr_parts.append(data)

        try:
            if cmd_timeout is not None:
                await asyncio.wait_for(_collect(), timeout=cmd_timeout)
            else:
                await _collect()
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            await self._close_ws(ws)
            _log.warning("exec_command timed out for pod %s", provider_ref)
            return ExecResult(
                exit_code=-1,
                stdout="",
                stderr="Command timed out",
                duration_ms=int((time.monotonic() - start) * 1000),
            )
        except Exception as exc:
            await self._close_ws(ws)
            raise BackendUnreachableError(
                f"Kubernetes exec stream failed for pod {provider_ref}: {type(exc).__name__}: {exc}"
            ) from exc
        await self._close_ws(ws)

        duration = int((time.monotonic() - start) * 1000)
        if state.error is not None:
            raise BackendUnreachableError(f"Kubernetes exec stream failed for pod {provider_ref}: {state.error}")
        exit_code, note = _resolve_exit_code(state.exit_payload)
        stderr_text = "".join(stderr_parts)
        if note is not None:
            stderr_text = f"{stderr_text}\n{note}" if stderr_text else note
        return ExecResult(
            exit_code=exit_code if exit_code is not None else -1,
            stdout="".join(stdout_parts),
            stderr=stderr_text,
            duration_ms=duration,
        )

    async def _open_exec(
        self,
        provider_ref: str,
        command: list[str],
        *,
        environment: dict[str, str] | None = None,
    ) -> Any:
        """Open the pods/exec WebSocket for *provider_ref* and return it ready.

        kubernetes-asyncio's exec call resolves in TWO awaits against a real
        cluster (aiohttp 3.14, kubernetes-asyncio 36.1.0):

        1. ``connect_get_namespaced_pod_exec(...)`` returns a coroutine that
           runs the request; awaiting it yields ``WsApiClient.request``'s
           return value — aiohttp's ``_WSRequestContextManager`` from
           ``ClientSession.ws_connect()``.
        2. Awaiting *that* context manager enters it and yields the actual
           ``ClientWebSocketResponse``.

        Returning after one await handed the caller the context manager, so
        every exec died with ``AttributeError: '_BaseRequestContextManager'
        object has no attribute 'recv'`` (FAR-1504). The websocket is
        returned bare (not via ``async with``): leaving that block would
        close the socket on return, and the callers close it explicitly
        through :meth:`_close_ws`.
        """
        ws_core = await self._get_ws_core()
        exec_command = self._command_with_environment(command, environment)
        try:
            context_manager = ws_core.connect_get_namespaced_pod_exec(
                provider_ref,
                self._namespace,
                command=exec_command,
                container=_CONTAINER_NAME,
                stderr=True,
                stdin=False,
                stdout=True,
                tty=False,
                _preload_content=False,
            )
            return await (await context_manager)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if getattr(exc, "status", None) == 404:
                raise UnknownRefError(
                    f"Kubernetes workspace pod {provider_ref!r} does not exist in namespace {self._namespace!r}"
                ) from exc
            raise BackendUnreachableError(
                f"Kubernetes exec subresource unreachable for pod {provider_ref!r}: {type(exc).__name__}: {exc}"
            ) from exc

    @staticmethod
    def _command_with_environment(command: list[str], environment: dict[str, str] | None) -> list[str]:
        """Apply *environment* to *command* via a POSIX-sh export prefix.

        The exec query has no env parameter in this client, so environment
        injection rides ``sh -c 'export K=v; ...; exec "$@"'`` — argument
        values are ``shlex.quote``'d (no injection) and invalid names are
        skipped with a log rather than breaking the exec.
        """
        exports: list[str] = []
        for key, value in (environment or {}).items():
            if not _ENV_NAME_RE.fullmatch(str(key)):
                _log.warning("Skipping invalid environment variable name: %r", key)
                continue
            exports.append(f"export {key}={shlex.quote(str(value))}")
        if not exports:
            return list(command)
        script = "; ".join(exports) + '; exec "$@"'
        return ["sh", "-c", script, "modulo-env", *command]

    async def _read_exec_frames(
        self,
        ws: Any,
        state: _ExecStreamState,
    ) -> AsyncIterator[tuple[str, str]]:
        """Read exec frames until the stream ends, yielding decoded chunks.

        An API/proxy death mid-stream records ``state.error`` (a stream
        ERROR, never a silent end with a fabricated success code) and ends
        the stream. Unknown channels with payload are routed to stderr so
        diagnostic output is never silently dropped.

        Reads go through ``receive()`` — aiohttp 3.14 removed
        ``ClientWebSocketResponse.recv()`` (FAR-1504), and a missing method
        would surface as a stream error rather than output.
        """
        while True:
            try:
                message = await ws.receive()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                state.error = _stream_error_message(exc)
                return
            message_type = getattr(message, "type", None)
            if message_type in (WSMsgType.CLOSED, WSMsgType.CLOSE):
                return
            if message_type == WSMsgType.ERROR:
                data = getattr(message, "data", None)
                state.error = _stream_error_message(
                    data if isinstance(data, BaseException) else RuntimeError(str(data))
                )
                return
            if message_type not in (WSMsgType.TEXT, WSMsgType.BINARY):
                continue  # PING/PONG and other control frames carry no output.
            raw = getattr(message, "data", None)
            payload = raw if isinstance(raw, str) else bytes(raw or b"").decode("utf-8", errors="replace")
            if not payload:
                continue
            channel = ord(payload[0])
            data = payload[1:]
            if not data:
                continue
            if channel == STDOUT_CHANNEL:
                yield ("stdout", data)
            elif channel == STDERR_CHANNEL:
                yield ("stderr", data)
            elif channel == ERROR_CHANNEL:
                state.exit_payload = data
            else:
                yield ("stderr", data)

    async def exec_command_stream(
        self,
        provider_ref: str,
        command: list[str],
        *,
        environment: dict[str, str] | None = None,
    ) -> ExecProcess:
        """Stream exec output as async chunks with a kill handle (D4 primitive).

        Kubernetes implementation of the ABC streaming primitive (same
        ``ExecProcess`` contract as E2B/Docker):
          - decoded :class:`ExecStreamChunk` values on ``chunks``, in order;
          - ``done`` fires when the stream ends (healthy or error);
          - ``exit_code`` stays ``None`` until the END of a HEALTHY stream —
            including a real non-zero command exit (a command failure is a
            result, not a stream error);
          - an API/proxy drop mid-stream (or a healthy end with no
            exit-status message) sets ``error`` and leaves ``exit_code``
            ``None`` — never a fabricated ``exit_code == 0``;
          - ``kill()`` closes the exec WebSocket best-effort (Docker parity:
            the stream ends; the container-side process is not signalled).
        """
        ws = await self._open_exec(provider_ref, command, environment=environment)
        state = _ExecStreamState()
        process = ExecProcess(chunks=None, kill=None)  # type: ignore[arg-type]

        async def _chunks() -> AsyncIterator[ExecStreamChunk]:
            exit_code: int | None = None
            error: str | None = None
            try:
                async for stream_name, data in self._read_exec_frames(ws, state):
                    yield ExecStreamChunk(stream=stream_name, data=data)
                # Only a healthy stream end resolves the exit status; a stream
                # ERROR must never be followed by an exit-code fabrication.
                if state.error is not None:
                    error = state.error
                else:
                    exit_code, error = _resolve_exit_code(state.exit_payload)
            finally:
                if error is not None:
                    process.error = error
                process.exit_code = exit_code
                process.done.set()

        process.chunks = _chunks()

        async def _kill() -> None:
            await self._close_ws(ws)

        process._kill = _kill
        return process

    async def _close_ws(self, ws: Any) -> None:
        """Close the exec WebSocket best-effort."""
        try:
            closed = ws.close()
            if asyncio.iscoroutine(closed):
                await closed
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.info("workspace exec stream close failed (best-effort)", exc_info=True)

    async def destroy_workspace(self, provider_ref: str) -> None:
        """Delete the workspace pod, best-effort (idempotent).

        Already-gone pods (404) are a logged no-op; every other failure is
        logged and swallowed (Docker parity).
        """
        if provider_ref not in self._workspaces:
            return
        self._workspaces.discard(provider_ref)
        await self._delete_pod_best_effort(provider_ref)

    async def destroy_workspace_by_ref(self, provider_ref: str) -> bool:
        """Destroy the pod given only its name (ADR 040 reclamation primitive).

        Contract (see :meth:`RuntimeProvider.destroy_workspace_by_ref`):
          - already-gone / foreign ref (no ``modulo.provider=kubernetes``
            label) -> idempotent success (``True``), the foreign pod is never
            touched;
          - returns ``True`` when the pod is confirmed gone (deleted by this
            call or already gone), ``False`` when the destroy could not be
            confirmed — failures are logged and swallowed (best-effort).
        """
        try:
            pod = await self._read_pod(provider_ref)
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("destroy_workspace_by_ref: failed to read pod %s", provider_ref)
            return False
        if pod is None:
            self._workspaces.discard(provider_ref)
            _log.debug("destroy_workspace_by_ref: pod %s already gone", provider_ref)
            return True
        labels = getattr(getattr(pod, "metadata", None), "labels", None) or {}
        if labels.get(_PROVIDER_LABEL) != _PROVIDER_LABEL_VALUE:
            _log.debug("destroy_workspace_by_ref: %s is not a Modulo workspace pod; leaving it", provider_ref)
            return True
        try:
            core = await self._get_core()
            await core.delete_namespaced_pod(name=provider_ref, namespace=self._namespace)
        except asyncio.CancelledError:
            raise
        except ApiException as exc:
            if exc.status == 404:
                self._workspaces.discard(provider_ref)
                return True
            _log.info("destroy_workspace_by_ref: delete failed for %s (HTTP %s)", provider_ref, exc.status)
            return False
        except Exception:
            _log.exception("destroy_workspace_by_ref: failed to delete pod %s", provider_ref)
            return False
        self._workspaces.discard(provider_ref)
        return True

    async def list_workspace_pods(self) -> list[WorkspacePodRef]:
        """List this deployment's Modulo workspace pods in the namespace.

        The listing primitive behind the workspace-orphan reconciler's
        Kubernetes source (FAR-1051 — "reconciler as single owner of K8s
        runs"). Two scoping layers, mirroring the Docker source's container
        filter:

        - server-side selector ``modulo.provider=kubernetes`` — never a
          foreign pod;
        - client-side deployment-identity match on the ``modulo.machine.id``
          ANNOTATION (pods carry the identity as an annotation, not a label),
          so two Modulo deployments sharing one namespace never reconcile
          each other's workspace pods.

        Any listing failure propagates: a configured-but-unreachable cluster
        must surface as a reported sweep failure, never a silent empty list.
        """
        core = await self._get_core()
        pods = await core.list_namespaced_pod(
            namespace=self._namespace,
            label_selector=f"{_PROVIDER_LABEL}={_PROVIDER_LABEL_VALUE}",
        )
        identity = self._deployment_identity()
        now = time.time()
        entries: list[WorkspacePodRef] = []
        for pod in getattr(pods, "items", None) or []:
            metadata = getattr(pod, "metadata", None)
            if metadata is None:
                continue
            annotations = getattr(metadata, "annotations", None) or {}
            if annotations.get(_MACHINE_ANNOTATION) != identity:
                continue
            labels = getattr(metadata, "labels", None) or {}
            try:
                created = float(labels.get(_CREATED_AT_LABEL, "0") or 0)
            except (TypeError, ValueError):
                created = 0.0
            ref = getattr(metadata, "name", "") or ""
            if not ref:
                _log.warning("list_workspace_pods: workspace pod without a name in namespace %s", self._namespace)
                continue
            entries.append(
                WorkspacePodRef(
                    ref=ref,
                    labels=dict(labels),
                    created_age_s=(now - created) if created else 0.0,
                )
            )
        return entries

    async def get_workspace_status(self, provider_ref: str) -> str:
        """Return the pod phase (``running`` / ``pending`` / ... ), ``terminated`` when gone."""
        try:
            pod = await self._read_pod(provider_ref)
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("Failed to get status for pod %s", provider_ref)
            return "unknown"
        if pod is None:
            return "terminated"
        phase, _note = self._pod_phase_and_note(pod)
        return phase or "unknown"

    async def read_log_tail(self, provider_ref: str, *, max_bytes: int) -> bytes:
        """Read the workspace container's log tail (ADR 040 primitive).

        K8s retains pod logs only while the pod exists (no post-destroy
        retention window on this tier), so a read after delete returns
        ``b""``. Best-effort probe contract: never raises — invalid ref,
        missing pod, RBAC and network failures all yield ``b""``. The
        ``max_bytes`` bound is a character slice on the decoded tail text
        before re-encoding (ABC contract).
        """
        if not isinstance(provider_ref, str) or not provider_ref or max_bytes <= 0:
            return b""
        try:
            core = await self._get_core()
            text = await core.read_namespaced_pod_log(
                name=provider_ref,
                namespace=self._namespace,
                container=_CONTAINER_NAME,
                tail_lines=_LOG_TAIL_LINES,
                timestamps=False,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.info("read_log_tail failed for pod %s (best-effort)", provider_ref, exc_info=True)
            return b""
        decoded = text if isinstance(text, str) else str(text or "")
        return decoded[-max_bytes:].encode("utf-8", errors="replace")

    async def apply_isolation(
        self,
        provider_ref: str,
        spec: WorkspaceSpec,
        policy: IsolationPolicy,
    ) -> str | None:
        """Enforce the ADR 040 isolation controls through the exec subresource.

        Controls this tier enforces (same script builders and step order as
        ``sandbox_policy.apply_sandbox_policy``: git-credential scope first,
        the one-PR guard next, the read-only seal LAST):
          - git-credential scoping (``scoped`` / ``none``, single- and
            multi-host) — enforcement-critical, raises on failure;
          - the read-only seal — enforcement-critical, raises on failure;
          - the FAR-1264 one-PR-per-run ``gh`` guard — best-effort via
            ``install_gh_pr_guard_via_exec``; its install status
            (``installed``/``pre_planted``/``absent``/``failed``) is returned
            per FAR-1315 (``None`` when the guard was not armed);
          - ``command_timeout`` bounds every step.

        Control this tier CANNOT enforce: the selected-mode egress allowlist
        and an egress ``none`` claim. Bounded egress on Kubernetes is the
        CUSTOMER's NetworkPolicy (opt-in; needs an enforcing CNI) — there is
        deliberately no in-pod egress mechanism here, so those requests raise
        the typed :class:`ProviderCapabilityUnsupportedError` naming the
        remediation. Never a silent downgrade.
        """
        egress = (policy.egress_policy or "").strip().lower()
        if egress == "none" or (egress == "selected" and policy.egress_allowlist):
            raise ProviderCapabilityUnsupportedError(
                "The Kubernetes tier cannot enforce "
                + ("an egress 'none' claim" if egress == "none" else "a selected-mode egress allowlist")
                + " inside the workspace pod: bounded egress on this tier is the customer's "
                "NetworkPolicy on the workspace namespace (opt-in, requires a CNI that enforces "
                "NetworkPolicy; FAR-1051). Configure it there and use a policy this tier can "
                "enforce."
            )

        # Lazy import (house convention): sandbox_policy is dependency-free,
        # but importing it pulls the pipeline_engine package __init__ — the
        # engine process already has it loaded when this runs.
        from modulo.core.pipeline_engine.sandbox_policy import (
            build_git_multi_host_script,
            build_git_none_script,
            build_git_scoped_script,
            build_read_only_script,
            install_gh_pr_guard_via_exec,
        )

        step_timeout = max(1, int(policy.command_timeout))

        if policy.git_credentials == "scoped":
            script = (
                build_git_multi_host_script(policy.allowed_hosts) if policy.allowed_hosts else build_git_scoped_script()
            )
            await self._run_enforcement_step(provider_ref, script, "git-credential scope", step_timeout)
        elif policy.git_credentials == "none":
            await self._run_enforcement_step(
                provider_ref, build_git_none_script(), "git-credential scope", step_timeout
            )

        guard_status: str | None = None
        if policy.single_pr_per_run:
            run_scope = str(spec.run_id) if spec.run_id is not None else None

            async def _exec(command: list[str]) -> Any:
                return await self.exec_command(provider_ref, command, cmd_timeout=step_timeout)

            result = await install_gh_pr_guard_via_exec(
                _exec,
                run_scope=run_scope,
                guard_owner=policy.guard_owner,
            )
            guard_status = result.status
            if result.detail:
                _log.warning("sandbox_policy.gh_guard_install_reported: %s", result.detail[:1000])

        if policy.read_only:
            # The seal runs LAST (it makes the workspace read-only) — parity
            # with apply_sandbox_policy's documented step order.
            await self._run_enforcement_step(provider_ref, build_read_only_script(), "read-only seal", step_timeout)

        return guard_status

    async def _run_enforcement_step(
        self,
        provider_ref: str,
        script: str,
        step_name: str,
        step_bound_s: int,
    ) -> None:
        """Run one enforcement-critical policy step; RAISES on any failure.

        Parity with ``apply_sandbox_policy``'s enforcement-critical semantics:
        a failed git-credential scope or read-only seal must dispatch a
        failure, never silently certify a deny-guarantee nothing enforced.
        """
        result = await self.exec_command(provider_ref, ["sh", "-c", script], cmd_timeout=step_bound_s)
        if result.exit_code != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise RuntimeProviderError(
                f"Kubernetes isolation step {step_name!r} failed for pod {provider_ref} "
                f"(exit {result.exit_code})" + (f": {detail[:200]}" if detail else "")
            )

    async def close(self) -> None:
        """Destroy tracked workspace pods, then close owned API clients (hub aclose).

        Per-workspace destroys are bounded (30s) so an unresponsive API
        server cannot stall teardown; client closes are best-effort with a
        logged warning on timeout/failure.
        """
        for provider_ref in tuple(self._workspaces):
            try:
                await asyncio.wait_for(
                    self.destroy_workspace(provider_ref),
                    timeout=_CLOSE_DESTROY_TIMEOUT_S,
                )
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                _log.warning(
                    "Timed out destroying workspace pod %s during close(); force-dropping it",
                    provider_ref,
                )
                self._workspaces.discard(provider_ref)
            except Exception:
                _log.exception("Failed to destroy workspace pod %s during close()", provider_ref)
        for client in (self._api_client, self._ws_api_client):
            if client is None:
                continue
            try:
                await asyncio.wait_for(client.close(), timeout=_CLOSE_DESTROY_TIMEOUT_S)
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                _log.warning("Timed out closing Kubernetes client during close(); force-dropping it")
            except Exception:
                _log.exception("Failed to close Kubernetes client during close()")
        self._api_client = None
        self._ws_api_client = None
        self._core_api = None
        self._ws_core_api = None
