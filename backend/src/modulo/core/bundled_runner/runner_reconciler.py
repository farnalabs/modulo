"""Docker workspace-orphan reconciler sweep (FAR-590, D4 — ADR 029 leak repair).

Lists labelled workspace containers from the deployment's Docker engine,
cross-references ACTIVE runs, and destroys orphans past the grace period.
Leak-repair WITHOUT the never-built ``WorkspaceLease``: the periodic sweep
is the whole repair mechanism.

Semantics committed by this delivery:
  - **Orphan = a labelled workspace container whose ``modulo.run.id`` label
    matches NO active (``running``/``awaiting_human``) run and whose age
    exceeds the 5-min grace period.**
  - **Fail-safe: ANY cross-reference query error aborts the sweep, destroys
    nothing, and emits** ``runner.reconciler.sweep_aborted``.
  - **Machine-scoped via the deployment-identity label**
    (``modulo.machine.id``, sourced from ``MODULO_RUNNER_MACHINE_ID`` with a
    hostname fallback): two Modulo deployments sharing one engine never
    destroy each other's workspaces.
  - **Log-only soak mode first** (settings flag
    ``runner_reconciler_destroy_enabled``, default False): orphans are
    logged loudly, not destroyed, until the operator flips the flag.
  - 24h max-lifetime backstop destroys labelled containers regardless of
    run state (``runner.workspace.reclaimed_max_lifetime``).
  - The destroy path RE-CHECKS run status before destroying and aborts with
    ``runner.reconciler.suspected_false_positive`` when the run is active
    again (the D4 rollback signal, detectable without post-hoc joins).
  - ``runner.reconciler.sweep_completed {scanned, orphans_destroyed}``
    closes every sweep.

Runs under the system-cron path in a bypass-RLS session (the sweep is
machine-scoped by deployment identity, not org-scoped).
"""

from __future__ import annotations

import logging
import os
import socket
import time
from dataclasses import dataclass
from typing import Any

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
    """Docker engine boundary for the reconciler (same env resolution as the provider)."""

    def __init__(self, docker_host: str | None) -> None:
        self._docker_host = docker_host
        self._client: Any = None

    async def _get_client(self) -> Any:
        if self._client is None:
            import aiodocker

            self._client = aiodocker.Docker(url=self._docker_host)
        return self._client

    async def list_labelled_workspaces(self) -> list[_LabelledContainer]:
        """List modulo-labelled workspace containers + their ages."""
        client = await self._get_client()
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
        client = await self._get_client()
        container = await client.containers.get(container_id)
        await container.delete(force=True)

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.close()
            finally:
                self._client = None


async def reconcile_runner_workspaces(
    async_engine: Any,
    *,
    grace_seconds: int = _GRACE_SECONDS_DEFAULT,
    max_lifetime_seconds: int = _MAX_LIFETIME_SECONDS_DEFAULT,
) -> dict[str, Any]:
    """Docker-orphan reconciler sweep (D1 cadence; system-cron path).

    Returns ``{"scanned": int, "orphans_destroyed": int}``. Raises
    :class:`ReconcilerSweepError` on failure (so SAQ's ``retries=2``
    engages) with the PARTIAL counts already achieved — a silently dead
    sweep must never re-open the labelled-container leak invisibly.
    """
    from modulo.settings import get_settings

    settings = get_settings()
    log_only = not bool(getattr(settings, "runner_reconciler_destroy_enabled", False))
    source = _DockerWorkspaceSource(_resolve_docker_host())

    scanned = 0
    orphans_destroyed = 0
    try:
        try:
            listed = await source.list_labelled_workspaces()
        except Exception as exc:
            _log.exception("runner.reconciler.sweep_aborted stage=container_list")
            raise ReconcilerSweepError(
                f"Bundled Runner orphan sweeper aborted listing containers: {exc}",
                scanned=0,
                destroyed=0,
            ) from exc

        active_run_ids, cross_ref_error = await _load_active_run_ids(async_engine)
        if cross_ref_error is not None:
            _log.error("runner.reconciler.sweep_aborted stage=cross_reference")
            raise ReconcilerSweepError(
                f"Bundled Runner orphan sweep aborted on cross-reference failure: {cross_ref_error}",
                scanned=len(listed),
                destroyed=0,
            )

        for container in listed:
            scanned += 1
            # 24h max-lifetime backstop FIRST (kill path per D1): a labelled
            # workspace container may never outlive one day, regardless of
            # run state — an active run cannot legitimately hold a workspace
            # anywhere near this long (sandbox_timeout caps at 3300s).
            if container.created_age_s > max_lifetime_seconds:
                _log.warning(
                    "runner.workspace.reclaimed_max_lifetime container=%s run=%s age=%ss",
                    container.id,
                    container.run_id,
                    int(container.created_age_s),
                )
                if not log_only:
                    await source.destroy_by_container_id(container.id)
                    orphans_destroyed += 1
                continue
            if container.run_id in active_run_ids:
                continue
            if container.created_age_s <= 0.0:
                # No creation marker on the label set: cannot establish a
                # grace age, so the container is never a destroy candidate.
                _log.info(
                    "runner.reconciler.no_created_marker container=%s run=%s",
                    container.id,
                    container.run_id,
                )
                continue
            if container.created_age_s < grace_seconds:
                continue
            if log_only:
                _log.info(
                    "runner.reconciler.orphan_detected (log-only soak) container=%s run=%s age=%ss",
                    container.id,
                    container.run_id,
                    int(container.created_age_s),
                )
                continue
            status_active = await _run_is_active(async_engine, container.run_id)
            if status_active:
                _log.warning(
                    "runner.reconciler.suspected_false_positive container=%s run=%s",
                    container.id,
                    container.run_id,
                )
                continue
            await source.destroy_by_container_id(container.id)
            orphans_destroyed += 1
            _log.warning(
                "runner.reconciler.orphan_destroyed container=%s run=%s",
                container.id,
                container.run_id,
            )
    finally:
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
