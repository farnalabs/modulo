"""Workspace-orphan reconciler sweep (FAR-590, D4 — ADR 029 leak repair).

Lists labelled workspace containers from the deployment's Docker engine AND
labelled workspace pods from the deployment's Kubernetes namespace (FAR-1051),
cross-references ACTIVE runs, and destroys orphans past the grace period.
Leak-repair WITHOUT the never-built ``WorkspaceLease``: the periodic sweep
is the whole repair mechanism.

Provider-neutral shape (FAR-1051): each tier contributes a workspace SOURCE
(list + destroy + close) behind the same structural boundary, so the sweep
logic — grace period, active-run spare, destroy-path re-check, max-lifetime
backstop, fail-safe abort — is written ONCE and applies to every tier. The
Docker source's behaviour is unchanged; the Kubernetes source reclaims
``modulo-ws-*`` pods through the provider's own ref-only destroy primitive.

Semantics committed by this delivery:
  - **Orphan = a labelled workspace whose ``modulo.run.id`` label
    matches NO active (``running``/``awaiting_human``) run and whose age
    exceeds the 5-min grace period.**
  - **Fail-safe: ANY cross-reference query error aborts the sweep, destroys
    nothing, and emits** ``runner.reconciler.sweep_aborted``.
  - **Machine-scoped via the deployment-identity label**
    (``modulo.machine.id``, sourced from ``MODULO_RUNNER_MACHINE_ID`` with a
    hostname fallback): two Modulo deployments sharing one engine (or one
    Kubernetes namespace) never destroy each other's workspaces.
  - **Log-only soak mode first** (settings flag
    ``runner_reconciler_destroy_enabled``, default False): orphans are
    logged loudly, not destroyed, until the operator flips the flag.
  - 24h max-lifetime backstop destroys labelled workspaces regardless of
    run state (``runner.workspace.reclaimed_max_lifetime``).
  - The destroy path RE-CHECKS run status before destroying and aborts with
    ``runner.reconciler.suspected_false_positive`` when the run is active
    again (the D4 rollback signal, detectable without post-hoc joins).
  - ``runner.reconciler.sweep_completed {scanned, orphans_destroyed}``
    closes every sweep.
  - **Engine-less skip (FAR-1201 follow-up)**: when the deployment has NO
    Docker endpoint at all (no ``MODULO_DOCKER_HOST``/``DOCKER_HOST``/
    ``DOCKER_CONTEXT``, no Docker context, and no local socket aiodocker
    would auto-detect), the Docker half is NOT APPLICABLE — it skips with
    ``runner.reconciler.skipped`` + an explicit reason instead of
    attempting (and permanently failing) an engine connection. The
    Kubernetes half has its own skip reason (provider not registered); a
    sweep reports the skip envelope only when NEITHER tier is applicable.

Runs under the system-cron path in a bypass-RLS session (the sweep is
machine-scoped by deployment identity, not org-scoped).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import socket
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

_log = logging.getLogger(__name__)

_RUN_ID_LABEL = "modulo.run.id"
_MACHINE_LABEL = "modulo.machine.id"
_GRACE_SECONDS_DEFAULT = 300
_MAX_LIFETIME_SECONDS_DEFAULT = 86400


class ReconcilerSweepError(RuntimeError):
    """The orphan sweep failed; the SAQ cron retries and /healthz sees it."""

    def __init__(self, message: str, *, scanned: int, destroyed: int) -> None:
        self.scanned = scanned
        self.destroyed = destroyed
        super().__init__(message)


@dataclass(frozen=True)
class _LabelledContainer:
    id: str
    run_id: str
    created_age_s: float


def deployment_identity() -> str:
    """The deployment-identity value (label rides every container filter)."""
    return os.environ.get("MODULO_RUNNER_MACHINE_ID") or socket.gethostname()


class _DockerWorkspaceSource:
    """Docker engine boundary for the reconciler (same env resolution as the provider).

    FAR-1038: the reconciler is the SECOND consumer of the deployment's Docker
    endpoint.  It applies the SAME TLS gate the provider applies at registration
    (single enforcement point) — otherwise a remote cleartext endpoint the
    provider refuses is still reachable through the orphan-sweep listing stream.
    """

    def __init__(self, docker_host: str | None) -> None:
        # Imported lazily (matching ``_get_client``); the validator is pure
        # env/URL logic in the neutral endpoint_tls module, so unlike the
        # concrete provider module it carries no aiodocker dependency.
        from modulo.core.runtime_provider.endpoint_tls import validate_docker_endpoint_tls

        validate_docker_endpoint_tls(docker_host)
        self._docker_host = docker_host
        self._client: Any = None

    def _get_client(self) -> Any:
        if self._client is None:
            import aiodocker

            self._client = aiodocker.Docker(url=self._docker_host)
        return self._client

    async def list_labelled_workspaces(self) -> list[_LabelledContainer]:
        """List modulo-labelled workspace containers + their ages."""
        client = self._get_client()
        filters = {"label": [f"{_MACHINE_LABEL}={deployment_identity()}", _RUN_ID_LABEL]}
        results = await client.containers.list(filters=filters)
        now = time.time()
        entries: list[_LabelledContainer] = []
        for container in results:
            # Real aiodocker DockerContainer objects carry the inspect payload
            # in ``_container`` (no public Labels attribute); test doubles set
            # ``Labels`` directly.
            raw = getattr(container, "_container", None)
            labels: dict[str, str] = {}
            if isinstance(raw, dict):
                labels = raw.get("Labels") or {}
            if not labels:
                labels = getattr(container, "Labels", None) or getattr(container, "_Labels", None) or {}
            run_id = labels.get(_RUN_ID_LABEL, "")
            if not run_id:
                continue
            created = float(labels.get("modulo.created_at", "0") or 0)
            entries.append(
                _LabelledContainer(
                    id=getattr(container, "Id", "") or getattr(container, "id", ""),
                    run_id=run_id,
                    created_age_s=(now - created) if created else 0.0,
                )
            )
        return entries

    async def destroy_by_container_id(self, container_id: str) -> None:
        client = self._get_client()
        container = await client.containers.get(container_id)
        await container.delete(force=True)

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.close()
            finally:
                self._client = None


class _WorkspaceSource(Protocol):
    """The provider-neutral workspace-source boundary the sweep drives.

    Structural, so the Docker engine source and the Kubernetes pod source
    (FAR-1051) satisfy it without inheriting from a shared base class — the
    sweep logic stays written once. ``destroy_by_container_id`` keeps its
    historical name; on the Kubernetes source the argument is the workspace
    POD NAME (the provider ref), which is what ``destroy_workspace_by_ref``
    addresses.
    """

    async def list_labelled_workspaces(self) -> list[_LabelledContainer]: ...

    async def destroy_by_container_id(self, container_id: str) -> None: ...

    async def close(self) -> None: ...


_KUBERNETES_PROVIDER_MODULE = "modulo.core.runtime_provider.k8s"


class _KubernetesWorkspaceSource:
    """Kubernetes pod boundary for the reconciler (FAR-1051).

    Reuses the provider rather than a second hand-rolled client: the
    provider owns the client configuration chain (in-cluster / kubeconfig),
    the namespace resolution (``MODULO_KUBERNETES_NAMESPACE``) and the
    deployment-identity annotation, and it owns the ref-only destroy
    primitive — which refuses any pod without the
    ``modulo.provider=kubernetes`` label, so the sweep can never delete a
    foreign pod. The concrete provider module is imported dynamically (the
    ``no-concrete-runtime-provider-imports`` contract keeps concrete
    providers inside the ``runtime_provider`` package boundary), and only
    when the Kubernetes half of the sweep is actually applicable.
    """

    def __init__(self) -> None:
        self._provider: Any = None

    def _get_provider(self) -> Any:
        if self._provider is None:
            import importlib

            module = importlib.import_module(_KUBERNETES_PROVIDER_MODULE)
            self._provider = module.KubernetesRuntimeProvider()
        return self._provider

    async def list_labelled_workspaces(self) -> list[_LabelledContainer]:
        """List this deployment's workspace pods + their ages.

        Pod labels are read through the provider's listing primitive (which
        applies the provider-label selector and the machine-identity
        annotation match); the run id and creation marker are read from the
        pod labels using THIS module's label vocabulary, exactly as the
        Docker source does for container labels. Pods without a
        ``modulo.run.id`` label are skipped for the same reason as on
        Docker: without a run id there is no cross-reference to fail-safe on.
        """
        pods = await self._get_provider().list_workspace_pods()
        entries: list[_LabelledContainer] = []
        for pod in pods:
            run_id = str(pod.labels.get(_RUN_ID_LABEL, "") or "")
            if not run_id:
                continue
            entries.append(
                _LabelledContainer(
                    id=str(pod.ref),
                    run_id=run_id,
                    created_age_s=float(pod.created_age_s or 0.0),
                )
            )
        return entries

    async def destroy_by_container_id(self, container_id: str) -> None:
        # destroy_workspace_by_ref is idempotent and label-guarded (a foreign
        # or already-gone pod is a logged no-op), so the sweep's decision is
        # the only gate that matters here. An UNCONFIRMED destroy (``False``)
        # is NOT swallowed: it raises so the sweep aborts with partial counts
        # and SAQ's retry + /healthz attribution engage — parity with the
        # Docker source, whose delete raises on failure.
        destroyed = await self._get_provider().destroy_workspace_by_ref(container_id)
        if not destroyed:
            raise RuntimeError(f"Kubernetes workspace pod {container_id!r} could not be confirmed destroyed")

    async def close(self) -> None:
        if self._provider is not None:
            provider, self._provider = self._provider, None
            await provider.close()


async def _reconcile_single_container(
    async_engine: Any,
    source: _WorkspaceSource,
    container: _LabelledContainer,
    *,
    active_run_ids: set[str],
    log_only: bool,
    grace_seconds: int,
    max_lifetime_seconds: int,
) -> bool:
    """Decide + act on ONE labelled workspace container.

    Returns True when the container was destroyed. The 24h max-lifetime
    backstop (kill path per D1) runs FIRST — a labelled workspace container
    may never outlive one day, regardless of run state (sandbox_timeout caps
    at 3300s). Skip order: active run -> no created marker (cannot establish
    a grace age, so never a destroy candidate) -> still inside the grace
    window -> log-only soak -> the destroy-path re-check (run still active
    => suspected false positive).
    """
    if container.created_age_s > max_lifetime_seconds:
        _log.warning(
            "runner.workspace.reclaimed_max_lifetime container=%s run=%s age=%ss",
            container.id,
            container.run_id,
            int(container.created_age_s),
        )
        if not log_only:
            await source.destroy_by_container_id(container.id)
            return True
        return False
    if container.run_id in active_run_ids:
        return False
    if container.created_age_s <= 0.0:
        # No creation marker on the label set: cannot establish a
        # grace age, so the container is never a destroy candidate.
        _log.info(
            "runner.reconciler.no_created_marker container=%s run=%s",
            container.id,
            container.run_id,
        )
        return False
    if container.created_age_s < grace_seconds:
        return False
    if log_only:
        _log.info(
            "runner.reconciler.orphan_detected (log-only soak) container=%s run=%s age=%ss",
            container.id,
            container.run_id,
            int(container.created_age_s),
        )
        return False
    status_active = await _run_is_active(async_engine, container.run_id)
    if status_active:
        _log.warning(
            "runner.reconciler.suspected_false_positive container=%s run=%s",
            container.id,
            container.run_id,
        )
        return False
    await source.destroy_by_container_id(container.id)
    _log.warning(
        "runner.reconciler.orphan_destroyed container=%s run=%s",
        container.id,
        container.run_id,
    )
    return True


async def reconcile_runner_workspaces(
    async_engine: Any,
    *,
    grace_seconds: int = _GRACE_SECONDS_DEFAULT,
    max_lifetime_seconds: int = _MAX_LIFETIME_SECONDS_DEFAULT,
) -> dict[str, Any]:
    """Workspace-orphan reconciler sweep (D1 cadence; system-cron path).

    Returns ``{"scanned": int, "orphans_destroyed": int}``. Raises
    :class:`ReconcilerSweepError` on failure (so SAQ's ``retries=2``
    engages) with the PARTIAL counts already achieved — a silently dead
    sweep must never re-open the labelled-workspace leak invisibly.

    FAR-1201 follow-up + FAR-1051: each tier has its own applicability check
    (see :func:`docker_endpoint_skip_reason` and
    :func:`kubernetes_endpoint_skip_reason`). A tier that is NOT APPLICABLE
    is skipped with ``runner.reconciler.skipped`` + its explicit reason
    instead of attempting (and permanently failing) a connection; a
    CONFIGURED-but-unreachable tier still raises: outage stays a reported
    failure, only "not applicable at all" skips. When NO tier is applicable
    the historical skip envelope is returned — ``{"scanned": 0,
    "orphans_destroyed": 0, "skipped": <docker reason>}`` — so the
    single-source contract every existing consumer keys on is unchanged;
    the other tier's reason is logged alongside it.
    """
    docker_skip = docker_endpoint_skip_reason()
    kubernetes_skip = await kubernetes_endpoint_skip_reason()
    if docker_skip is not None and kubernetes_skip is not None:
        if kubernetes_skip != docker_skip:
            _log.info("runner.reconciler.skipped reason=%s scope=kubernetes", kubernetes_skip)
        _log.info("runner.reconciler.skipped reason=%s", docker_skip)
        return {"scanned": 0, "orphans_destroyed": 0, "skipped": docker_skip}
    if docker_skip is not None:
        _log.info("runner.reconciler.skipped reason=%s scope=docker", docker_skip)
    if kubernetes_skip is not None:
        _log.info("runner.reconciler.skipped reason=%s scope=kubernetes", kubernetes_skip)

    from modulo.settings import get_settings

    settings = get_settings()
    log_only = not bool(getattr(settings, "runner_reconciler_destroy_enabled", False))
    sources: list[tuple[str, _WorkspaceSource]] = []
    if docker_skip is None:
        sources.append(("docker", _DockerWorkspaceSource(_resolve_docker_host())))
    if kubernetes_skip is None:
        sources.append(("kubernetes", _KubernetesWorkspaceSource()))

    scanned = 0
    orphans_destroyed = 0
    source_by_name: dict[str, _WorkspaceSource] = dict(sources)
    try:
        # List EVERY applicable tier before touching anything: a listing
        # failure on the second tier must still destroy nothing (the
        # fail-safe abort is per-sweep, not per-tier).
        listed: list[tuple[str, _LabelledContainer]] = []
        for source_name, source in sources:
            try:
                entries = await source.list_labelled_workspaces()
            except Exception as exc:
                _log.exception("runner.reconciler.sweep_aborted stage=container_list source=%s", source_name)
                raise ReconcilerSweepError(
                    f"Bundled Runner orphan sweeper aborted listing {source_name} workspaces: {exc}",
                    scanned=0,
                    destroyed=0,
                ) from exc
            listed.extend((source_name, entry) for entry in entries)

        active_run_ids, cross_ref_error = await _load_active_run_ids(async_engine)
        if cross_ref_error is not None:
            _log.error("runner.reconciler.sweep_aborted stage=cross_reference")
            raise ReconcilerSweepError(
                f"Bundled Runner orphan sweep aborted on cross-reference failure: {cross_ref_error}",
                scanned=len(listed),
                destroyed=0,
            )

        for source_name, container in listed:
            scanned += 1
            try:
                destroyed = await _reconcile_single_container(
                    async_engine,
                    source_by_name[source_name],
                    container,
                    active_run_ids=active_run_ids,
                    log_only=log_only,
                    grace_seconds=grace_seconds,
                    max_lifetime_seconds=max_lifetime_seconds,
                )
            except Exception as exc:
                # A destroy failure aborts the sweep on EVERY tier (Docker's
                # delete raises too) and is surfaced as ReconcilerSweepError
                # with the partial counts, so SAQ's retries and /healthz
                # attribution engage rather than the failure vanishing into a
                # log line while the sweep reports the orphan as destroyed.
                _log.exception(
                    "runner.reconciler.sweep_aborted stage=destroy source=%s container=%s",
                    source_name,
                    container.id,
                )
                raise ReconcilerSweepError(
                    f"Bundled Runner orphan sweeper aborted destroying {source_name} workspace {container.id}: {exc}",
                    scanned=scanned,
                    destroyed=orphans_destroyed,
                ) from exc
            if destroyed:
                orphans_destroyed += 1
    finally:
        for _, source in sources:
            await source.close()
    _log.info(
        "runner.reconciler.sweep_completed scanned=%d orphans_destroyed=%d log_only=%s",
        scanned,
        orphans_destroyed,
        log_only,
    )
    return {"scanned": scanned, "orphans_destroyed": orphans_destroyed}


def _resolve_docker_host() -> str | None:
    # The reconciler uses the same Docker endpoint chain as the provider:
    # MODULO_DOCKER_HOST first, then DOCKER_HOST, else None (the engine's
    # local socket). The raw-socket operator override is documented in
    # docs/security/.
    return os.environ.get("MODULO_DOCKER_HOST") or os.environ.get("DOCKER_HOST")


def _local_docker_socket_paths() -> tuple[Path, ...]:
    """aiodocker's POSIX socket auto-detection paths (``_sock_search_paths``)."""
    paths: list[Path] = [Path("/run/docker.sock"), Path("/var/run/docker.sock")]
    with contextlib.suppress(OSError, RuntimeError):  # no home dir resolvable — skip the last path
        paths.append(Path.home() / ".docker" / "run" / "docker.sock")
    return tuple(paths)


def _windows_docker_engine_pipe_exists() -> bool:
    """True when the Docker Desktop named pipe exists (Windows only)."""
    if sys.platform != "win32":
        return False
    try:
        return Path(r"\\.\pipe\docker_engine").exists()
    except OSError:
        return False


def _default_docker_socket_present() -> bool:
    """True when aiodocker's platform-default local endpoint is present.

    Parity with aiodocker's own host resolution: search the socket paths
    first on every platform, then the Windows named pipe. aiodocker's
    win32 ``https://127.0.0.1:2376`` fallback is a TLS-gated vestige, not
    a usable endpoint without explicit configuration, so it does not count
    as "configured" here.
    """
    if any(path.is_socket() for path in _local_docker_socket_paths()):
        return True
    return _windows_docker_engine_pipe_exists()


def _docker_config_path() -> Path:
    """The Docker CLI config file aiodocker reads for ``currentContext``."""
    return Path.home() / ".docker" / "config.json"


def _context_endpoint_configured() -> bool:
    """True when aiodocker's Docker-context resolution is in play here.

    Mirrors ``Docker._get_docker_context_endpoint`` exactly, including its
    failure modes: ``DOCKER_CONTEXT`` env first — when SET (even to
    ``"default"`` or empty) it suppresses the config-file lookup entirely —
    else ``currentContext`` from ``~/.docker/config.json`` (absent /
    ``"default"`` / ``null`` means "no context"). A config file that is
    malformed, not a JSON object, or carries a non-default ``currentContext``
    of any type counts as configured: aiodocker raises at client
    construction in those cases (``DockerContextInvalidError`` /
    ``AttributeError``), and that error must surface as a failed sweep, not
    be skipped away.
    """
    ctx = os.environ.get("DOCKER_CONTEXT")
    if ctx is not None:
        return ctx != "default"
    try:
        raw = _docker_config_path().read_bytes()
    except (OSError, RuntimeError):
        return False
    try:
        data = json.loads(raw)
    except ValueError:
        return True
    if not isinstance(data, dict):
        return True
    # Any non-default value (including a non-string) makes aiodocker raise at
    # construction (DockerContextInvalidError / AttributeError) — count it as
    # configured so that error surfaces as a failed sweep, not a skip.
    current = data.get("currentContext")
    return current is not None and current != "default"


def docker_endpoint_skip_reason() -> str | None:
    """Why the DOCKER half of the orphan sweep is NOT APPLICABLE here, or ``None``.

    FAR-1201 follow-up + FAR-1051 (provider-neutral sweep): this reason
    scopes the Docker source only — the sweep as a whole runs whenever ANY
    tier is applicable, and the Kubernetes half carries its own reason
    (:func:`kubernetes_endpoint_skip_reason`). The Docker source skips only
    when NO Docker endpoint can be resolved at all — mirroring aiodocker's
    own host-resolution chain
    (explicit url → Docker context → auto-detected local socket), which is
    exactly the case where ``aiodocker.Docker(url=None)`` raises at
    construction (the permanently-degraded engine-unreachable check on
    engine-less deployments such as EKS with no ``MODULO_DOCKER_HOST``/
    ``DOCKER_HOST`` and no local socket). Corroborated by ``build_hub``
    (``runtime_provider``): the Docker provider only REGISTERS when an env
    endpoint is set, so an engine-less deployment creates no workspace
    containers for the Docker source to reconcile.

    A CONFIGURED-but-unreachable endpoint returns ``None``: outage must keep
    reporting engine-unreachable, never skip. Only "nothing configured at
    all" skips.
    """
    if _resolve_docker_host():
        return None
    if _context_endpoint_configured():
        return None
    if _default_docker_socket_present():
        return None
    return (
        "no Docker endpoint configured (MODULO_DOCKER_HOST, DOCKER_HOST and "
        "DOCKER_CONTEXT all unset, no Docker context selected, no local "
        "Docker socket) — set MODULO_DOCKER_HOST to enable the "
        "workspace-orphan sweep"
    )


async def kubernetes_endpoint_skip_reason() -> str | None:
    """Why the KUBERNETES half of the sweep is NOT APPLICABLE here, or ``None``.

    FAR-1051: the sweep is applicable exactly when the deployment would
    REGISTER the Kubernetes runtime provider, so the check delegates to the
    registration matrix itself (:func:`modulo.core.runtime_provider.build_hub`
    — ``MODULO_KUBERNETES_ENABLED`` gating plus the kubernetes-asyncio SDK
    presence) instead of re-implementing the flag semantics here and letting
    the two drift. That is also why an unregistered provider is a SKIP and
    not a failure: no provider means no workspace pods were ever created for
    this sweep to reclaim.

    ``None`` means a provider IS registered — after which a configured-but-
    unreachable cluster still surfaces as a reported sweep failure (the
    listing call raises), never a skip. The probe's ephemeral hub is disposed
    via :meth:`RuntimeProviderHub.aclose` before returning: a freshly built
    provider holds no client connections until first use (the Kubernetes
    client configuration is lazy), so the close is the ADR 029 bookkeeping
    pass — it releases any client a registration did open and never touches
    the cluster (reviewer feedback on PR #1277: close the probe hub rather
    than leak it).
    """
    from modulo.core.runtime_provider import build_hub

    try:
        provider_hub = build_hub()
    except Exception:
        _log.exception("runner.reconciler.kubernetes_probe_failed")
        return "the runtime-provider hub could not be built — see logs; the workspace-pod sweep cannot run"
    try:
        if provider_hub.get("kubernetes") is None:
            return (
                "the Kubernetes runtime provider is not registered — set "
                "MODULO_KUBERNETES_ENABLED to an enabling value (and install the "
                "kubernetes-asyncio SDK) to enable the workspace-pod orphan sweep"
            )
        return None
    finally:
        await provider_hub.aclose()


async def _load_active_run_ids(
    async_engine: Any,
) -> tuple[set[str], BaseException | None]:
    """Cross-reference active runs in one bypass-RLS query (fail-safe on error)."""
    from sqlalchemy import text

    try:
        async with async_engine.connect() as conn:
            result = await conn.execute(text("SELECT id FROM runs WHERE status IN ('running', 'awaiting_human')"))
            return {str(row[0]) for row in result.all()}, None
    except Exception as exc:
        return set(), exc


async def _run_is_active(
    async_engine: Any,
    run_id: str,
) -> bool:
    """The destroy-path re-check: run still active -> suspected false positive."""
    from sqlalchemy import text

    async with async_engine.connect() as conn:
        result = await conn.execute(
            text("SELECT 1 FROM runs WHERE id = :rid AND status IN ('running', 'awaiting_human')"),
            {"rid": str(run_id)},
        )
        return result.fetchone() is not None
