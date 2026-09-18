"""DB capacity monitor + 98% hard-stop gate (FAR-425/426).

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

Resilience is the first constraint, not the second: a failed capacity query
must NEVER crash a run dispatch or a health probe. Any measurement error makes
the function return ``capacity_percent=None`` / ``alert_level="ok"`` so the
caller treats capacity as unknown and allows the work through.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from modulo.settings import Settings, get_settings

_log = logging.getLogger(__name__)

# Alert-level thresholds (percent of configured capacity).
ALERT_FULL_PCT = 98.0
ALERT_CRITICAL_PCT = 90.0
ALERT_WARN_PCT = 80.0

_VALID_MODES = frozenset({"fixed", "elastic", "disabled"})


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
            "capacity_percent": float | None,  # None if no capacity configured
            "mode": "fixed" | "elastic" | "disabled",
            "alert_level": "ok" | "warn" | "critical" | "full",
            "used_bytes": int,
            "capacity_bytes": int | None,     # None for elastic/disabled
        }

    ``used_bytes`` is ``pg_database_size(current_database())``. ``capacity_bytes``
    is ``db_capacity_bytes`` (settings) for ``mode="fixed"`` and ``None`` for
    elastic/disabled. ``capacity_percent`` is ``used/capacity*100`` clamped to
    0-100, or ``None`` when no capacity is configured. ``alert_level`` is
    derived from the percent; an unknown/measurement-failed percent reads as
    ``"ok"`` so a probe never fails on an outage alarm's own measurement.
    """
    settings = get_settings()
    mode = _normalise_mode(settings.db_capacity_mode)

    used_bytes = 0
    capacity_bytes: int | None = None
    measured: int | None = None
    if mode != "disabled":
        measured = await _query_used_bytes(engine)
        used_bytes = measured if measured is not None else 0
        if mode == "fixed":
            capacity_bytes = settings.db_capacity_bytes

    # Only report a percentage when the measurement succeeded AND a capacity is
    # configured — a failed query or unset capacity reads as unknown (None).
    percent: float | None = None
    if measured is not None and capacity_bytes and capacity_bytes > 0:
        percent = (measured / capacity_bytes) * 100.0
        percent = round(max(0.0, min(100.0, percent)), 1)

    return {
        "capacity_percent": percent,
        "mode": mode,
        "alert_level": _alert_level(percent),
        "used_bytes": used_bytes,
        "capacity_bytes": capacity_bytes,
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
