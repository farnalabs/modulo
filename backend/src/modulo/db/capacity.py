"""DB capacity monitor + 98% hard-stop gate (FAR-425/426, auto-detect FAR-1882).

FAR-425/426: a 100%-full Postgres volume caused a full outage. This module

* measures how full the application DB is and classifies an alert level
  (``ok``/``warn``/``critical``/``full``) that the frontend banner polls via
  ``/api/v1/admin/db-capacity``; and
* provides the mode-gated hard-stop that REFUSES new run creation when a
  *fixed* (self-hosted) DB is at/over ``db_capacity_hard_stop_pct``.

Mode-gating: ``fixed`` (self-hosted, enforced) hard-stops; ``elastic``
(Aurora / horizontally-scaled — advisory only) and ``disabled`` never
hard-stop. The gate only ever fires for a FRESH run — never a resume,
retention sweep or admin operation, which all bypass it by never entering the
new-run creation boundary.

Capacity sources (FAR-1882 + FAR-1992): for ``mode="fixed"`` the capacity no
longer has to be configured — :func:`_resolve_capacity` auto-detects it, trying
the ``DB_CAPACITY_BYTES`` override, then a filesystem probe of the Postgres
volume mounted read-only at ``db_capacity_path``, then an optional
provider/platform probe (FAR-1992) for deployments whose DB volume is on a
separate machine/volume (Fly: the app and the Postgres machine are separate
apps). Whichever source resolves first (and yields a capacity that can
physically hold the database) wins and is reported as ``source``. The old
contract is preserved: an explicit ``DB_CAPACITY_BYTES`` still wins and is never
second-guessed, and when nothing resolves the monitor reports
``capacity_percent=None`` / ``alert_level="ok"`` and the hard-stop stays INERT
(fail-open, unchanged).

The platform probe is INJECTED (see :func:`set_platform_capacity_probe`) rather
than imported: its concrete providers live in the infra/provider layer
(``modulo.core.capacity_providers``) so ``modulo.db`` keeps its zero-import
contract with ``modulo.core``.

Resilience is the first constraint, not the second: a failed capacity query or
a failed probe must NEVER crash a run dispatch or a health probe. Any
measurement error makes the function return ``capacity_percent=None`` /
``alert_level="ok"`` so the caller treats capacity as unknown and allows the
work through.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from modulo.settings import Settings, get_settings

_log = logging.getLogger(__name__)

# Alert-level thresholds (percent of configured capacity).
ALERT_FULL_PCT = 98.0
ALERT_CRITICAL_PCT = 90.0
ALERT_WARN_PCT = 80.0

_VALID_MODES = frozenset({"fixed", "elastic", "disabled"})

# Capacity-source selection (FAR-1882). "auto" tries the sources in order; the
# others pin a single one; "disabled" resolves nothing.
_VALID_SOURCES = frozenset({"auto", "env", "filesystem", "disabled"})
# The auto chain (FAR-1882 + FAR-1992): the operator's explicit override wins
# first, then a filesystem probe of a mounted Postgres volume, then the optional
# provider/platform probe for deployments where the DB volume is NOT mounted
# into the app (e.g. Fly, where the app and the Postgres machine are separate
# apps). ``none`` is never a candidate — an unresolved chain is reported as
# ``source="none"`` with the gate INERT.
_AUTO_SOURCE_ORDER = ("env", "filesystem", "platform")

# Sources this module DERIVES by probing the runtime (rather than an operator
# stating the value). Only these are sanity-checked against the measured
# database size: an explicit ``DB_CAPACITY_BYTES`` override is the operator
# asserting the capacity, and second-guessing it is a bug (FAR-1882). The
# platform probe is derived too, so the impossibility guard applies to it
# (FAR-1992).
_AUTO_DETECTED_SOURCES = frozenset({"filesystem", "platform"})

_none_source_warned = False


class PlatformCapacityProbe(Protocol):
    """Port for a provider/platform capacity probe (FAR-1992).

    A provider-agnostic capability: return the byte capacity of the volume that
    holds the application database, or ``None`` when the provider cannot resolve
    one (credentials/config absent, volume ambiguous, API error). Implementations
    live in the infra/provider layer (``modulo.core.capacity_providers``) and are
    installed at application startup via :func:`set_platform_capacity_probe` —
    ``modulo.db`` itself never imports that layer, preserving the ``db → core``
    import-linter contract.

    ``probe_capacity_bytes`` MUST be fail-open: any failure returns ``None``
    rather than raising, so the source chain falls through to ``none``.
    """

    @property
    def name(self) -> str:
        """Short provider name (e.g. ``"fly"``), for logs/provenance diagnostics."""
        ...

    async def probe_capacity_bytes(self) -> int | None:
        """Return the volume capacity in bytes, or ``None`` if unresolvable."""
        ...


# The installed provider/platform probe (FAR-1992): ``None`` until application
# startup injects one, so a process that never wires it keeps the FAR-1882
# behaviour exactly (filesystem probe, then ``none`` — inert).
_platform_probe: PlatformCapacityProbe | None = None


def set_platform_capacity_probe(probe: PlatformCapacityProbe | None) -> None:
    """Install (or clear) the process-wide provider/platform capacity probe.

    Called at application startup (the API lifespan and the SAQ worker startup
    hook) with a probe built by ``modulo.core.capacity_providers``. Passing
    ``None`` clears it. This is the ONLY seam by which the ``platform`` source
    becomes available; the DB layer stays free of any provider import.
    """
    global _platform_probe
    _platform_probe = probe


@dataclass(frozen=True)
class _ResolvedCapacity:
    """A resolved capacity and usage numerator, plus the source that produced them."""

    capacity_bytes: int | None
    used_bytes: int | None
    source: str


class StorageExhaustedError(RuntimeError):
    """Raised at NEW-run creation when a fixed DB is at/over the hard-stop.

    A domain exception raised from :func:`enforce_capacity_gate`; the API layer
    converts it to HTTP 503 ``urn:problem:modulo:storage_exhausted``.
    """


async def _query_used_bytes(engine: AsyncEngine) -> int | None:
    """Return ``pg_database_size(current_database())`` for the app DB.

    ``pg_database_size`` reports the current database's on-disk size (the one
    ``current_database()`` resolves to, i.e. the app DB). Returns ``None`` on
    any failure (non-Postgres backend, DB unreachable, permission) — the
    caller treats unknown as safe.
    """
    try:
        async with engine.connect() as conn:
            result = await conn.execute(text("SELECT pg_database_size(current_database())"))
            return int(result.scalar_one())
    except Exception:
        _log.warning("db_capacity: pg_database_size query failed for engine %r", engine, exc_info=True)
        return None


def _normalise_mode(mode: str | None) -> str:
    normalized = (mode or "disabled").strip().lower()
    return normalized if normalized in _VALID_MODES else "disabled"


def _normalise_source(source: str | None) -> str:
    """Normalise ``db_capacity_source``; an unrecognised value falls back to ``auto``."""
    normalized = (source or "auto").strip().lower()
    return normalized if normalized in _VALID_SOURCES else "auto"


def _disk_usage(path: str) -> Any:
    """Indirection over ``shutil.disk_usage`` (the cross-platform ``statvfs``).

    Kept as a module-level function so tests can patch it without touching
    ``shutil`` globally.
    """
    return shutil.disk_usage(path)


def _probe_filesystem_capacity(path: str) -> tuple[int, int] | None:
    """Return ``(total_bytes, free_bytes)`` for ``path``'s filesystem, else ``None``.

    Best-effort: a missing/unmounted path or a filesystem error returns ``None``
    so the caller falls through to the next source. ``free`` is the space
    available to the (unprivileged) app process, matching ``df``.
    """
    try:
        usage = _disk_usage(path)
    except (OSError, ValueError):
        _log.debug("db_capacity: filesystem probe of %r failed", path, exc_info=True)
        return None
    total = int(usage.total)
    free = int(usage.free)
    if total <= 0:
        return None
    return total, max(0, free)


async def _try_source(
    candidate: str,
    settings: Settings,
    measured_pg_size: int | None,
) -> tuple[int, int | None] | None:
    """Resolve ONE candidate source to ``(capacity_bytes, usage numerator)``.

    The numerator is the measured database size for ``env`` and ``platform``
    (neither source can see the volume's other occupants); for ``filesystem`` it
    is the volume's own ``total - free``. ``None`` means the source did not apply
    (unset override, unmounted path, probe failure/unconfigured).
    """
    if candidate == "env":
        capacity = settings.db_capacity_bytes
        if capacity is None or capacity <= 0:
            return None
        return capacity, measured_pg_size
    if candidate == "filesystem":
        probed = _probe_filesystem_capacity(settings.db_capacity_path)
        if probed is None:
            return None
        total, free = probed
        return total, max(0, total - free)
    if candidate == "platform":
        return await _try_platform_source(measured_pg_size)
    return None


async def _try_platform_source(
    measured_pg_size: int | None,
) -> tuple[int, int | None] | None:
    """Resolve the injected provider/platform probe, defensively (FAR-1992).

    No probe installed (or config absent ⇒ the built probe is ``None``) resolves
    nothing, so the chain falls through to ``none`` and the gate stays inert.
    The probe is fail-open by contract; the ``except`` here is belt-and-braces so
    a misbehaving provider can never crash a run dispatch or a health probe.
    The numerator is the measured database size — the platform API exposes the
    volume's capacity, not its consumption.
    """
    probe = _platform_probe
    if probe is None:
        return None
    try:
        capacity = await probe.probe_capacity_bytes()
    except Exception:
        _log.warning(
            "db_capacity: platform probe %r raised - discarding its reading",
            getattr(probe, "name", "?"),
            exc_info=True,
        )
        return None
    if capacity is None or capacity <= 0:
        return None
    return capacity, measured_pg_size


def _warn_no_capacity_source(path: str, source_pref: str) -> None:
    """Warn ONCE, loudly, that no capacity source resolved (the gate is INERT).

    The message names the SELECTED source preference rather than asserting that
    ``DB_CAPACITY_BYTES`` is unset - it is frequently set when the preference is
    ``"disabled"`` or when the override was discarded as implausible, and a
    warning that misstates why nothing resolved sends an operator down the
    wrong path.
    """
    global _none_source_warned
    if _none_source_warned:
        return
    _none_source_warned = True
    _log.warning(
        "db_capacity: no capacity source resolved (DB_CAPACITY_SOURCE=%s: no "
        "usable DB_CAPACITY_BYTES override, no readable volume at "
        "DB_CAPACITY_PATH=%r, no usable platform probe) - the %.0f%% hard-stop "
        "stays INERT. Set DB_CAPACITY_BYTES, mount the Postgres data volume "
        "read-only at DB_CAPACITY_PATH, or configure DB_CAPACITY_PLATFORM_PROVIDER.",
        source_pref,
        path,
        ALERT_FULL_PCT,
    )


async def _resolve_capacity(
    settings: Settings,
    source_pref: str,
    measured_pg_size: int | None,
) -> _ResolvedCapacity:
    """Resolve the capacity + usage numerator for ``mode="fixed"`` (FAR-1882).

    ``source_pref`` is the normalised ``db_capacity_source``: ``"auto"`` tries
    the sources in :data:`_AUTO_SOURCE_ORDER`; the others pin a single one;
    ``"disabled"`` resolves nothing. Every probe is fail-open.

    An AUTO-DETECTED source (``filesystem``, ``platform``) is sanity-checked: a
    volume whose total is *smaller than the database it holds* is physically
    impossible, so that reading is the wrong volume — it is discarded (with a
    warning) and resolution continues. The check is an impossibility, not a
    margin, so a legitimately small-but-valid volume still resolves and still
    arms the gate. An explicit ``DB_CAPACITY_BYTES`` override (``env``) is the
    operator asserting the capacity and is NEVER second-guessed.
    """
    if source_pref == "disabled":
        _warn_no_capacity_source(settings.db_capacity_path, source_pref)
        return _ResolvedCapacity(None, None, "none")

    candidates = _AUTO_SOURCE_ORDER if source_pref == "auto" else (source_pref,)
    for candidate in candidates:
        resolved = await _try_source(candidate, settings, measured_pg_size)
        if resolved is None:
            continue
        capacity_bytes, used_bytes = resolved
        if candidate in _AUTO_DETECTED_SOURCES and measured_pg_size is not None and capacity_bytes < measured_pg_size:
            _log.warning(
                "db_capacity: %s source reported capacity %d bytes, smaller than "
                "the database it holds (%d bytes) - physically impossible, so it "
                "is the wrong volume; discarding it and continuing (FAR-1882)",
                candidate,
                capacity_bytes,
                measured_pg_size,
            )
            continue
        return _ResolvedCapacity(capacity_bytes, used_bytes, candidate)

    _warn_no_capacity_source(settings.db_capacity_path, source_pref)
    return _ResolvedCapacity(None, None, "none")


def _alert_level(percent: float | None) -> str:
    if percent is None:
        return "ok"
    if percent >= ALERT_FULL_PCT:
        return "full"
    if percent >= ALERT_CRITICAL_PCT:
        return "critical"
    if percent >= ALERT_WARN_PCT:
        return "warn"
    return "ok"


async def db_capacity_status(engine: AsyncEngine) -> dict[str, Any]:
    """Return the live DB capacity status.

    Returns EXACTLY::

        {
            "capacity_percent": float | None,  # None if no capacity resolved
            "mode": "fixed" | "elastic" | "disabled",
            "alert_level": "ok" | "warn" | "critical" | "full",
            "used_bytes": int,
            "capacity_bytes": int | None,     # None for elastic/disabled/no source
            "source": "env" | "filesystem" | "platform" | "none",
        }

    ``capacity_bytes`` (and therefore ``capacity_percent``) is resolved for
    ``mode="fixed"`` by :func:`_resolve_capacity` per ``db_capacity_source``
    (FAR-1882 / FAR-1992): the ``DB_CAPACITY_BYTES`` override, the
    ``DB_CAPACITY_PATH`` filesystem probe of the Postgres volume, or the
    injected provider/platform probe (``source="platform"``) for a deployment
    whose DB volume is not mounted into the app. ``source`` names the winner, or
    ``"none"`` when nothing resolved. Elastic/disabled modes never resolve a
    capacity.

    ``used_bytes`` is the resolved usage numerator: ``pg_database_size(...)``
    for the env and platform sources, or the volume's ``total - free`` for the
    filesystem source (which also counts WAL/temp, so it is the honest companion
    to that source's ``capacity_bytes``). ``capacity_percent`` is ``used/capacity*100``
    clamped to 0-100, or ``None`` when no capacity resolved or the source
    produced no usable numerator. ``alert_level`` is derived from the percent;
    an unknown/measurement-failed percent reads as ``"ok"`` so a probe never
    fails on an outage alarm's own measurement.
    """
    settings = get_settings()
    mode = _normalise_mode(settings.db_capacity_mode)
    source_pref = _normalise_source(settings.db_capacity_source)

    used_bytes = 0
    capacity_bytes: int | None = None
    source = "none"
    resolved = _ResolvedCapacity(None, None, "none")
    measured: int | None = None
    if mode != "disabled":
        measured = await _query_used_bytes(engine)
        used_bytes = measured if measured is not None else 0
        if mode == "fixed":
            resolved = await _resolve_capacity(settings, source_pref, measured)
            capacity_bytes = resolved.capacity_bytes
            source = resolved.source
            if resolved.used_bytes is not None:
                used_bytes = resolved.used_bytes

    # Only report a percentage when a capacity resolved AND the source produced
    # a usable numerator (env needs the DB measurement; filesystem has its own).
    # A failed query or unresolved source reads as unknown (None).
    percent: float | None = None
    if capacity_bytes and capacity_bytes > 0 and resolved.used_bytes is not None:
        raw = (resolved.used_bytes / capacity_bytes) * 100.0
        percent = round(max(0.0, min(100.0, raw)), 1)

    return {
        "capacity_percent": percent,
        "mode": mode,
        "alert_level": _alert_level(percent),
        "used_bytes": used_bytes,
        "capacity_bytes": capacity_bytes,
        "source": source,
    }


def capacity_hard_stop(settings: Settings, status: dict[str, Any]) -> bool:
    """Pure decision — ``True`` when a NEW run must be refused.

    Enforces ONLY for ``mode="fixed"``, when bypass is NOT set, and the live
    percent is at/over ``db_capacity_hard_stop_pct``. Elastic/disabled/unknown
    capacity and bypass always return ``False`` (advisory / allow).
    """
    if _normalise_mode(settings.db_capacity_mode) != "fixed":
        return False
    if settings.db_capacity_bypass:
        return False
    percent: float | None = status.get("capacity_percent")
    if percent is None:
        return False
    return percent >= settings.db_capacity_hard_stop_pct


async def enforce_capacity_gate(
    *,
    settings: Settings | None = None,
    engine: AsyncEngine | None = None,
) -> None:
    """Raise :class:`StorageExhaustedError` when a NEW run must be refused.

    The single chokepoint the new-run creation path calls BEFORE persisting a
    run. Fail-open: any measurement error logs a warning and returns without
    raising, so a broken capacity probe can never block run creation. Only a
    deliberate ``fixed``-mode, at/over-hard-stop, non-bypass state raises.

    The measurement runs on ``engine`` (default: the process-wide shared
    engine), never on the caller's pending transaction — capacity is a
    property of the database, not of whatever the current session is mid-write.
    """
    status: dict[str, Any]
    try:
        settings = settings or get_settings()
        if engine is None:
            from modulo.db.session import get_shared_engine

            engine = get_shared_engine()
        status = await db_capacity_status(engine)
    except Exception:
        _log.warning("db_capacity: capacity gate measurement failed (allowing run)", exc_info=True)
        return

    if capacity_hard_stop(settings, status):
        pct = status.get("capacity_percent")
        raise StorageExhaustedError(
            "Storage capacity exhausted "
            f"({pct:g}% of configured capacity, hard-stop {settings.db_capacity_hard_stop_pct:g}%). "
            "Export/clear old runs via Run Retention, or run checkpoint housekeeping. "
            "Set DB_CAPACITY_BYPASS=1 to force."
        )
