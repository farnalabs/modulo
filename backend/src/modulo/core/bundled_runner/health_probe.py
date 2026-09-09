"""Per-machine runner health probe (FAR-591, D5 — ADR 029 system-cron path).

Every 60 seconds (per-machine SAQ system cron, ``runner_health_probe``) the
deployment probes its Docker engine THROUGH the socket-proxy — engine
reachability, the pinned runner image presence, and the engine's reported
resources (``/info`` CPU/memory for the engine-resource preflight) — and
upserts one :class:`~modulo.db.models.runner_probe_cache.RunnerProbeCache`
row per (organisation, machine).

The Runners page + node editor read ONLY this cache; a synchronous probe
never runs on the request path. Multi-machine deployments get per-machine
rows (one shared row would collapse per-machine engine health); the strip
aggregates worst-of across the org's rows.

Cross-surface (D5): a healthy→unreachable transition emits BOTH an
error-dashboard entry (``signal=runner_unavailable`` via
:func:`modulo.core.error_tracking.emit_signal_event`) and an in-app
notification (``runner.*`` namespace, category ``runner``). Transitions are
detected against the PREVIOUS cached row, so a dead probe can never loop
alerts, and an unreachable state that persists re-alerts only after a
recovery in between.

System cron: uses the modulo_system role (LOGIN, BYPASSRLS) for cross-org
access — modulo_app is NOBYPASSRLS. The probe fails-open per org (one bad
org never aborts the whole tick) but re-raises on engine-level infrastructure
errors so SAQ's ``retries=2`` engages.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from modulo.db.bundled_runner_template import is_placeholder_bundled_runner_image_ref

_log = logging.getLogger(__name__)

#: Per-container limits (ADR 029) — the preflight multiplies the org's
#: concurrency cap by these when assessing host headroom.
PER_CONTAINER_CPU = 1.0
PER_CONTAINER_MEM_MB = 1024

NOTIFICATION_CATEGORY = "runner"
NOTIFICATION_ACTION_URL = "/admin/runners/concurrency"


class EngineProbeOutcome:
    """The raw result of probing ONE engine (machine) — org-independent parts."""

    __slots__ = ("engine_info", "error", "reachable")

    def __init__(
        self,
        *,
        reachable: bool,
        engine_info: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        self.reachable = reachable
        self.engine_info = engine_info or {}
        self.error = error


def _resolve_docker_host() -> str | None:
    # Same endpoint chain as the provider + reconciler: MODULO_DOCKER_HOST
    # first (the filtered socket-proxy), then DOCKER_HOST, else None (the
    # engine's local socket).
    return os.environ.get("MODULO_DOCKER_HOST") or os.environ.get("DOCKER_HOST")


class _EngineBoundary:
    """Docker engine boundary for the probe (aiodocker, same env resolution)."""

    def __init__(self, docker_host: str | None) -> None:
        self._docker_host = docker_host
        self._client: Any = None

    async def _get_client(self) -> Any:
        if self._client is None:
            import aiodocker

            self._client = aiodocker.Docker(url=self._docker_host)
        return self._client

    async def probe_engine(self) -> EngineProbeOutcome:
        """Ping the engine + read ``/info`` (CPU count + total memory)."""
        try:
            client = await self._get_client()
            info = await client.system.info()
            cpu_count = info.get("NCPU")
            mem_total = info.get("MemTotal")
            engine_info: dict[str, Any] = {}
            if isinstance(cpu_count, int) and cpu_count > 0:
                engine_info["cpu_count"] = cpu_count
            if isinstance(mem_total, int) and mem_total > 0:
                engine_info["mem_total_mb"] = mem_total // (1024 * 1024)
            return EngineProbeOutcome(reachable=True, engine_info=engine_info)
        except Exception as exc:
            return EngineProbeOutcome(reachable=False, error=str(exc)[:500])

    async def image_present(self, image_ref: str) -> bool | None:
        """Inspect ONE pinned image; ``None`` when the engine is unreachable."""
        try:
            client = await self._get_client()
            await client.images.inspect(image_ref)
            return True
        except Exception as exc:
            # A 404 means genuinely absent; any other failure (engine died
            # between the ping and the inspect) also reports absent-safe
            # False — the worst-of aggregation renders the warning either way.
            _log.debug("runner_probe.image_inspect_failed ref=%s error=%s", image_ref, exc)
            return False

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.close()
            finally:
                self._client = None


def strip_state_for_row(
    *,
    engine_reachable: bool,
    images_present: bool | None,
    probed_at: Any,
    now: Any,
) -> str:
    """Map ONE cached probe row to its strip state (pure; unit-tested).

    States (worst-of per machine): ``healthy`` | ``image_not_pulled`` |
    ``engine_unreachable`` | ``stale``. Staleness dominates — a dead probe
    can never leave "healthy" on screen.
    """
    from modulo.db.crud.runner_probe import probe_is_stale

    if probed_at is None or probe_is_stale(probed_at, now):
        return "stale"
    if not engine_reachable:
        return "engine_unreachable"
    if images_present is False:
        return "image_not_pulled"
    return "healthy"


def aggregate_strip_state(states: list[str]) -> str:
    """Worst-of aggregation across machine rows (pure; unit-tested).

    Order: stale > engine_unreachable > image_not_pulled > healthy. An empty
    list (no probe rows at all — e.g. the probe never ran) reads ``stale``:
    the strip renders "status unknown (last checked …)" rather than a
    confident green.
    """
    if not states:
        return "stale"
    for state in ("stale", "engine_unreachable", "image_not_pulled"):
        if state in states:
            return state
    return "healthy"


async def _emit_unreachable_transition(
    session: Any,
    org_id: Any,
    machine_id: str,
    error: str | None,
) -> None:
    """Error-dashboard entry + in-app notification on healthy→unreachable."""
    from modulo.core.error_tracking import emit_signal_event
    from modulo.db.crud.notifications import create_notification

    message = f"Bundled Runner engine on machine '{machine_id}' became unreachable (probe error: {error or 'unknown'})."
    try:
        await emit_signal_event(
            session,
            org_id,
            signal="runner_unavailable",
            pipeline_id=None,
            message=message,
            level="error",
        )
    except Exception:
        _log.exception("runner_probe.transition_error_event_failed org=%s", org_id)
    try:
        await create_notification(
            session,
            org_id=org_id,
            scope="admin",
            level="error",
            category=NOTIFICATION_CATEGORY,
            title="Runner engine unreachable",
            body=message,
            action_url=NOTIFICATION_ACTION_URL,
        )
    except Exception:
        _log.exception("runner_probe.transition_notification_failed org=%s", org_id)


async def run_runner_health_probe(
    session_factory: Any,
    *,
    machine_id: str | None = None,
    engine_boundary: _EngineBoundary | None = None,
) -> dict[str, Any]:
    """The system-cron tick: probe the engine, upsert per-(org, machine) rows.

    Returns ``{"orgs_probed": int, "machines": [machine_id], "transitions": int}``.
    Org-level failures are logged and skipped (fail-open per org — one broken
    org never blocks the fleet's probe); the tick itself only re-raises when
    the session factory itself fails, so SAQ's retries engage on real
    infrastructure errors, not per-org noise.
    """
    from modulo.core.bundled_runner.runner_reconciler import deployment_identity
    from modulo.db.crud.runner_probe import (
        get_runner_probe_cache,
        list_org_image_refs,
        list_orgs_with_runner_profiles,
        upsert_runner_probe_cache,
    )

    identity = machine_id or deployment_identity()
    boundary = engine_boundary or _EngineBoundary(_resolve_docker_host())
    transitions = 0
    orgs_probed = 0
    try:
        outcome = await boundary.probe_engine()
        # Probe each org's pinned images ONLY while the engine is reachable —
        # an unreachable engine makes every image check meaningless.
        checked_images: dict[str, bool | None] = {}
        started = time.monotonic()
        async with session_factory() as session, session.begin():
            org_ids = await list_orgs_with_runner_profiles(session)
            for org_id in org_ids:
                try:
                    image_refs = await list_org_image_refs(session, org_id)
                    if outcome.reachable:
                        for ref in image_refs:
                            if ref not in checked_images:
                                checked_images[ref] = (
                                    None
                                    if is_placeholder_bundled_runner_image_ref(ref)
                                    else await boundary.image_present(ref)
                                )
                        if not image_refs:
                            images_present = None
                        else:
                            images_present = all(bool(checked_images.get(ref)) for ref in image_refs)
                    else:
                        images_present = None

                    previous = await get_runner_probe_cache(session, org_id=org_id, machine_id=identity)
                    was_reachable = previous.engine_reachable if previous is not None else True

                    await upsert_runner_probe_cache(
                        session,
                        org_id=org_id,
                        machine_id=identity,
                        engine_reachable=outcome.reachable,
                        images_present=images_present,
                        image_checks={ref: checked_images.get(ref) for ref in image_refs},
                        engine_info=outcome.engine_info,
                        probe_error=outcome.error,
                    )
                    orgs_probed += 1

                    if was_reachable and not outcome.reachable:
                        transitions += 1
                        await _emit_unreachable_transition(session, org_id, identity, outcome.error)
                except Exception:
                    _log.exception("runner_probe.org_failed org=%s", org_id)
        _log.info(
            "runner_probe.tick_completed machine=%s reachable=%s orgs=%d duration_ms=%d",
            identity,
            outcome.reachable,
            orgs_probed,
            int((time.monotonic() - started) * 1000),
        )
    finally:
        await boundary.close()
    return {
        "machine_id": identity,
        "reachable": outcome.reachable,
        "orgs_probed": orgs_probed,
        "transitions": transitions,
    }
