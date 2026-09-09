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
recovery in between. The VERY FIRST probe finding the engine down also
emits the transition (no previous row defaults to "was reachable") — a
deployment that boots with a dead engine is alerted once, not silently
green.

Tick shape (idle-in-transaction hygiene, qa F15): the engine probe and the
image inspects run BEFORE any DB transaction is open; the write transaction
is scoped to the cache upserts alone.

Failure semantics (qa F2/F3): each org's upsert + transition emission runs
inside a SAVEPOINT (``begin_nested``) so one poisoned org rolls back only
itself — the other orgs still land their rows in the same tick. A
per-org *infrastructure* failure (SQLAlchemyError) marks the org failed and
is RE-RAISED at the end of the tick so SAQ's ``retries=2`` engages (the
partial counts are persisted by the saq_worker wrapper first); non-infra
per-org errors stay fail-open (logged, org skipped, tick continues). The
60s cadence is itself the retry for fail-open orgs.

System cron: uses the modulo_system role (LOGIN, BYPASSRLS) for cross-org
access — modulo_app is NOBYPASSRLS.
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Any, Literal

from aiohttp import ClientTimeout
from sqlalchemy.exc import SQLAlchemyError

from modulo.db.bundled_runner_template import is_placeholder_bundled_runner_image_ref

_log = logging.getLogger(__name__)

#: Per-container limits (ADR 029) — the preflight multiplies the org's
#: concurrency cap by these when assessing host headroom.
PER_CONTAINER_CPU = 1.0
PER_CONTAINER_MEM_MB = 1024

NOTIFICATION_CATEGORY = "runner"
NOTIFICATION_ACTION_URL = "/admin/runners/concurrency"

#: Matches ``userinfo@`` inside any URL embedded in an error string
#: (credential-bearing ``MODULO_DOCKER_HOST`` — tcp://user:pass@host:port).
_URL_USERINFO_RE = re.compile(r"(?<=//)[^/?#\s:@]+:[^/?#\s:@]+@")

#: qa F17: the strip states are a closed contract shared with the wire
#: schema (routes/runners derives its Pydantic Literals from this union).
StripState = Literal["healthy", "engine_unreachable", "image_not_pulled", "stale"]


def scrub_url_credentials(text: str) -> str:
    """Replace any ``user:password@`` userinfo in URL occurrences with ``***@``.

    Docker URLs may embed registry/proxy credentials
    (``tcp://user:pass@host:2375``); aiohttp/aiodocker error strings include
    the URL verbatim, so every persisted ``probe_error`` and every log line
    derived from an engine exception must pass through here first.
    """
    return _URL_USERINFO_RE.sub("***@", text)


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

            # Bounded timeouts (qa F10): without one a hung socket-proxy
            # stalls the inspect until the SAQ job timeout (120s) kills the
            # whole tick — every org's row then ages toward "stale" with no
            # recorded transition. A bounded client turns the hang into a
            # recorded unreachable (with a real transition alert) in ≤10s,
            # comfortably under the job timeout.
            self._client = aiodocker.Docker(
                url=self._docker_host,
                timeout=ClientTimeout(total=10, connect=5),
            )
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
            return EngineProbeOutcome(
                reachable=False,
                error=scrub_url_credentials(str(exc))[:500],
            )

    async def image_present(self, image_ref: str) -> bool | None:
        """Inspect ONE pinned image.

        ``True`` present; ``False`` the registry/engine answered 404 (the
        digest genuinely has no image); ``None`` UNKNOWN — any other failure
        (engine died between the ping and the inspect, proxy hiccup) must
        not be reported as absent (qa F4): a transient failure would
        permanently flip the strip to ``image_not_pulled`` until a later
        probe recovers it.
        """
        try:
            client = await self._get_client()
            await client.images.inspect(image_ref)
            return True
        except Exception as exc:
            status = getattr(exc, "status", None)
            if status == 404:
                _log.debug("runner_probe.image_absent ref=%s", image_ref)
                return False
            _log.debug(
                "runner_probe.image_inspect_failed ref=%s error=%s",
                image_ref,
                scrub_url_credentials(str(exc)),
            )
            return None

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.close()
            finally:
                self._client = None


def aggregate_image_presence(states: list[bool | None]) -> bool | None:
    """Worst-of aggregation of per-image check results (pure; unit-tested).

    ``False`` (a definitively absent image) dominates; otherwise any
    ``None`` (unknown — transient inspect failure) keeps the aggregate
    unknown rather than raising a false ``image_not_pulled`` alert (qa F4);
    all-True reads True; an empty list (no real images pinned) is unknown.
    """
    if not states:
        return None
    if any(state is False for state in states):
        return False
    if any(state is None for state in states):
        return None
    return True


def strip_state_for_row(
    *,
    engine_reachable: bool,
    images_present: bool | None,
    probed_at: Any,
    now: Any,
) -> StripState:
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


def aggregate_strip_state(states: list[StripState]) -> StripState:
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

    Returns ``{"machine_id", "reachable", "orgs_probed", "orgs_failed",
    "transitions"}`` (qa F18: documented return shape).

    Three phases (qa F15 — the engine is slow/unreliable, the DB is not):

    1. READ (no engine calls inside): the orgs holding runner profiles and
       each org's pinned image refs.
    2. ENGINE (no DB transaction open): probe ``/info`` + inspect every
       distinct non-placeholder image ref once, deduped across orgs.
    3. WRITE (one transaction): prune orphaned machine rows (qa F8), then
       per-org upsert inside a SAVEPOINT (qa F2 — a poisoned org rolls back
       only itself). Per-org infrastructure errors (SQLAlchemyError) mark
       the org failed and are re-raised at the end so SAQ's ``retries=2``
       engages; non-infra per-org errors stay fail-open.

    A healthy→unreachable transition is emitted on the FIRST down probe
    (no previous row defaults to "was reachable") — a deployment that boots
    with a dead engine alerts once instead of sitting silently green.
    """
    from modulo.core.bundled_runner.runner_reconciler import deployment_identity
    from modulo.db.crud.runner_probe import (
        PROBE_RETENTION_SECONDS,
        get_runner_probe_cache,
        list_org_image_refs,
        list_orgs_with_runner_profiles,
        prune_stale_runner_probe_rows,
        upsert_runner_probe_cache,
    )

    identity = machine_id or deployment_identity()
    boundary = engine_boundary or _EngineBoundary(_resolve_docker_host())
    transitions = 0
    orgs_probed = 0
    orgs_failed = 0
    first_infra_error: SQLAlchemyError | None = None
    try:
        started = time.monotonic()

        # Phase 1 (read): orgs + pinned refs. Short read, no engine calls.
        async with session_factory() as read_session:
            org_ids = await list_orgs_with_runner_profiles(read_session)
            org_refs: dict[Any, list[str]] = {}
            for org_id in org_ids:
                org_refs[org_id] = await list_org_image_refs(read_session, org_id)

        # Phase 2 (engine, NO transaction open): probe + deduped inspects.
        outcome = await boundary.probe_engine()
        checked_images: dict[str, bool | None] = {}
        if outcome.reachable:
            for ref in sorted({r for refs in org_refs.values() for r in refs}):
                if not is_placeholder_bundled_runner_image_ref(ref):
                    checked_images[ref] = await boundary.image_present(ref)

        # Phase 3 (write): one tx; per-org SAVEPOINT.
        async with session_factory() as session, session.begin():
            # qa F8: prune (org, machine) rows the probe has not refreshed
            # within the retention window — a decommissioned machine's corpse
            # row must not pin the strip to "stale" forever (the read side
            # filters by the same window; this deletes the dead data).
            await prune_stale_runner_probe_rows(
                session,
                retention_seconds=PROBE_RETENTION_SECONDS,
            )
            for org_id, image_refs in org_refs.items():
                try:
                    # qa F2: the SAVEPOINT confines one org's failure — a
                    # poisoned statement rolls back to the savepoint and the
                    # outer transaction (other orgs' upserts) stays usable.
                    async with session.begin_nested():
                        real_refs = [r for r in image_refs if not is_placeholder_bundled_runner_image_ref(r)]
                        # qa F4: aggregate over NON-placeholder refs only; a
                        # placeholder-only org has no inspectable image and
                        # must read unknown (None), never a permanent false
                        # "image not pulled".
                        images_present: bool | None = (
                            aggregate_image_presence([checked_images.get(r) for r in real_refs])
                            if outcome.reachable
                            else None
                        )

                        previous = await get_runner_probe_cache(session, org_id=org_id, machine_id=identity)
                        was_reachable = previous.engine_reachable if previous is not None else True

                        await upsert_runner_probe_cache(
                            session,
                            org_id=org_id,
                            machine_id=identity,
                            engine_reachable=outcome.reachable,
                            images_present=images_present,
                            image_checks={ref: checked_images.get(ref) for ref in real_refs},
                            engine_info=outcome.engine_info,
                            probe_error=outcome.error,
                        )
                        orgs_probed += 1

                        if was_reachable and not outcome.reachable:
                            transitions += 1
                            await _emit_unreachable_transition(session, org_id, identity, outcome.error)
                except SQLAlchemyError as exc:
                    # Infra error for THIS org only: the savepoint rolled it
                    # back, the outer tx is still usable. Record and continue;
                    # re-raised at the end (qa F3) so SAQ retries engage.
                    orgs_failed += 1
                    if first_infra_error is None:
                        first_infra_error = exc
                    _log.exception("runner_probe.org_infra_failed org=%s", org_id)
                except Exception:
                    # Non-infra per-org error: fail-open (logged, skipped).
                    _log.exception("runner_probe.org_failed org=%s", org_id)
        _log.info(
            "runner_probe.tick_completed machine=%s reachable=%s orgs=%d failed=%d duration_ms=%d",
            identity,
            outcome.reachable,
            orgs_probed,
            orgs_failed,
            int((time.monotonic() - started) * 1000),
        )
    finally:
        await boundary.close()
    if first_infra_error is not None:
        # qa F3: an infra failure must reach SAQ's retry machinery, not be
        # swallowed by the per-org fail-open. The partial counts are already
        # in the return path of the saq_worker wrapper's stats persist.
        raise first_infra_error
    return {
        "machine_id": identity,
        "reachable": outcome.reachable,
        "orgs_probed": orgs_probed,
        "orgs_failed": orgs_failed,
        "transitions": transitions,
    }
