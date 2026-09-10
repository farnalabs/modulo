"""Worker guest-memory capture + advisory alarm (FAR-776).

Periodic (5-min cron) capture of `/proc/meminfo` and cgroup v1 memory
counters for durable OOM forensics and guest-memory pressure alerting.

Design decisions:
- **5-min cadence**: piggybacks on the existing system-cron infrastructure
  (SAQ ``CronJob``), no new async-task lifecycle. Chosen because the
  crash-window counters (``memory.failcnt``, ``memory.oom_control``) do NOT
  survive cgroup v1 restarts — periodic snapshots to structured logs are
  the only durable capture.
- **Best-effort reads**: every ``/proc`` / ``/sys`` read is wrapped in
  try-except; a missing path or permission error skips that field and
  never crashes the worker.
- **Alarm transport**: a LOUD structured log line (``worker.guest_memory_alarm``,
  searchable by token) when thresholds are breached. No readiness gate
  impact — advisory only.
- **Thresholds** (would have fired before the 2026-09-09 near-miss):
  - ``MemAvailable < 20% of MemTotal``
  - ``Committed_AS > CommitLimit``

The Grafana dashboards in ``configs/grafana/`` are file-provisioned
(not live-managed by the modulo-telemetry app), so alerting is surfaced
in-process via structured logs rather than Grafana alert rules.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

# Thresholds — would have fired before the 2026-09-09 worker near-miss
# (Committed_AS ~1.25 GB > CommitLimit 0.96 GB, no swap, ~755 MB available).
MEMORY_AVAILABLE_LOW_RATIO = 0.20  # MemAvailable < 20% of MemTotal
COMMIT_OVERCOMMIT = True  # Committed_AS > CommitLimit

# Paths — injectable for testing; production reads the real FS
_PROC_MEMINFO_PATH = "/proc/meminfo"
_CGROUP_MEMORY_FAILCNT = "/sys/fs/cgroup/memory/memory.failcnt"
_CGROUP_MEMORY_OOM_CONTROL = "/sys/fs/cgroup/memory/memory.oom_control"
_CGROUP_MEMORY_PEAK = "/sys/fs/cgroup/memory/memory.peak"

_MACHINE_ID = os.environ.get("FLY_MACHINE_ID") or os.environ.get("HOSTNAME") or "unknown"


def _read_proc_meminfo() -> dict[str, int]:
    """Parse ``/proc/meminfo`` into ``{name_kb: value_kb}``.

    Only the fields needed by the capture are extracted.  Returns a partial
    dict if some fields are missing — callers must check for keys.
    """
    result: dict[str, int] = {}
    try:
        with Path(_PROC_MEMINFO_PATH).open() as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2 and parts[0] in (
                    "MemTotal:",
                    "MemAvailable:",
                    "Committed_AS:",
                    "CommitLimit:",
                ):
                    result[parts[0].rstrip(":")] = int(parts[1])
    except OSError:
        pass
    return result


def _read_cgroup_counter(path: str) -> int | None:
    """Read a single integer from a cgroup v1 counter file."""
    try:
        with Path(path).open() as f:
            return int(f.read().strip())
    except OSError:
        return None


def _read_cgroup_oom_control() -> dict[str, Any]:
    """Parse ``memory.oom_control`` (cgroup v1) into a dict.

    Returns a partial dict on read failure — callers check keys.
    """
    result: dict[str, Any] = {}
    try:
        with Path(_CGROUP_MEMORY_OOM_CONTROL).open() as f:
            for line in f:
                if ":" in line:
                    key, value = line.split(":", 1)
                    result[key.strip()] = value.strip()
    except OSError:
        pass
    return result


def capture_memory_stats() -> dict[str, Any]:
    """Best-effort snapshot of guest memory state.

    Reads ``/proc/meminfo`` (MemTotal, MemAvailable, Committed_AS,
    CommitLimit) and cgroup v1 counters (failcnt, oom_control, peak).
    Every read is best-effort: a missing path or parse error skips that
    field and never raises.
    """
    stats: dict[str, Any] = {"machine_id": _MACHINE_ID}

    meminfo = _read_proc_meminfo()
    for key in ("MemTotal", "MemAvailable", "Committed_AS", "CommitLimit"):
        if key in meminfo:
            stats[key] = meminfo[key]

    failcnt = _read_cgroup_counter(_CGROUP_MEMORY_FAILCNT)
    if failcnt is not None:
        stats["memory_failcnt"] = failcnt

    oom = _read_cgroup_oom_control()
    if "oom_kill" in oom:
        stats["memory_oom_kill"] = oom["oom_kill"]

    peak = _read_cgroup_counter(_CGROUP_MEMORY_PEAK)
    if peak is not None:
        stats["memory_peak"] = peak

    return stats


def evaluate_memory_alarm(stats: dict[str, Any]) -> str | None:
    """Evaluate memory thresholds and return an alarm reason or ``None``.

    Checks:
    - ``MemAvailable < 20% of MemTotal`` — low free-memory headroom.
    - ``Committed_AS > CommitLimit`` — overcommit (the condition that
      would have triggered before the 2026-09-09 near-miss).

    Returns the alarm reason string (for logging) or ``None`` if healthy.
    """
    mem_total = stats.get("MemTotal")
    mem_available = stats.get("MemAvailable")
    committed_as = stats.get("Committed_AS")
    commit_limit = stats.get("CommitLimit")

    if (
        mem_total is not None
        and mem_available is not None
        and mem_total > 0
        and mem_available < mem_total * MEMORY_AVAILABLE_LOW_RATIO
    ):
        return (
            f"MemAvailable={mem_available}kB < 20% of MemTotal={mem_total}kB "
            f"({mem_available / mem_total * 100:.1f}% available)"
        )

    if committed_as is not None and commit_limit is not None and commit_limit > 0 and committed_as > commit_limit:
        return (
            f"Committed_AS={committed_as}kB > CommitLimit={commit_limit}kB "
            f"({committed_as / commit_limit * 100:.1f}% of limit)"
        )

    return None


async def memory_monitor_cron(_ctx: dict[str, Any]) -> dict[str, Any]:
    """System cron — periodic guest-memory capture + advisory alarm (FAR-776).

    Runs every 5 min via the system worker. Captures memory stats to a
    structured log line and fires a LOUD alarm when thresholds indicate
    guest-memory pressure. Advisory only — never fails the worker, never
    gates readiness.
    """
    stats = capture_memory_stats()
    alarm = evaluate_memory_alarm(stats)

    _log.info("worker.memory_stats", extra=stats)

    if alarm:
        _log.warning(
            "worker.guest_memory_alarm",
            extra={**stats, "alarm": alarm},
        )

    return {"status": "ok", "alarm": alarm is not None, **stats}
