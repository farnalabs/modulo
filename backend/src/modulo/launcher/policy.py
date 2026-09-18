"""Single source of supervision policy constants (FAR-674, ADR 031).

Every policy/timing knob the supervision stack enforces lives here:

* the in-app supervisor's tick, restart-backoff schedule and sliding-window
  crash cap (``SupervisorKnobs`` defaults reference these — this module is
  the single source, not a parallel copy);
* the ordered-teardown priorities and the terminal-degraded contract
  (exit code, backtrace ring size, post-upgrade degraded window);
* the container-entrypoint's sliding crash guard (``SLIDING_*`` — extracted
  verbatim from the inline magic numbers ``deploy/fly/entrypoint.sh`` used
  to carry, so the shell wrapper's behaviour is identical);
* the systemd unit's co-designed restart budget (``SERVICE_*``).

The module is ALSO the shell-sourceable form of those constants: running
``python3 -m modulo.launcher.policy`` prints a POSIX-shell ``KEY=value``
file (:func:`format_shell_constants`) that ``deploy/fly/entrypoint.sh``
sources at boot instead of carrying its own magic numbers. The wheel ships
this module and the all-in-one image installs the project, so the
entrypoint's ``python3 -m modulo.launcher.policy`` import path works.

Values here are PRODUCTION defaults; tests shrink knobs directly on the
dataclasses (never by editing this module).
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# In-app supervisor (SupervisorKnobs production defaults)
# ---------------------------------------------------------------------------

TICK_SECONDS = 1.0
RESTART_BACKOFF_INITIAL = 1.0
RESTART_BACKOFF_MAX = 30.0
# Sliding window for child crashes: reaching CRASH_CAP crashes inside the
# window trips the terminal degraded state (the cap trips ON the Nth crash).
CRASH_WINDOW_SECONDS = 600.0
CRASH_CAP = 5
PG_FAST_SHUTDOWN_TIMEOUT = 15.0
SHUTDOWN_GRACE_SECONDS = 10.0
HEALTH_CHECK_TIMEOUT = 60.0
HEALTH_CHECK_INTERVAL = 0.25

# Ordered teardown: lower priority tears down first (SAQ -> Postgres -> Redis).
TEARDOWN_PRIORITY_SAQ = 0
TEARDOWN_PRIORITY_POSTGRES = 1
TEARDOWN_PRIORITY_REDIS = 2

# ---------------------------------------------------------------------------
# Terminal degraded (crash cap exhausted)
# ---------------------------------------------------------------------------

# Exit code the launcher uses for a TERMINAL DEGRADED exit. The systemd unit
# lists it in SuccessExitStatus so the OS manager leaves the service STOPPED
# instead of fighting the launcher's own crash cap (75 = EX_TEMPFAIL).
DEGRADED_EXIT_CODE = 75
# Bounded ring of persisted crash records (child, exit code, backtrace tail).
DEGRADED_BACKTRACE_RING = 10
# Post-upgrade degraded window (N = the crash-cap window): a degrade within
# this many seconds of a recorded upgrade boot gets the refuse/restore
# guidance attached to the degraded message (the upgrade machinery itself is
# FAR-675; this slice only wires the detection + message).
UPGRADE_DEGRADED_WINDOW_SECONDS = CRASH_WINDOW_SECONDS

# ---------------------------------------------------------------------------
# Container entrypoint sliding crash guard (deploy/fly/entrypoint.sh)
# ---------------------------------------------------------------------------

# Extracted VERBATIM from entrypoint.sh's former inline values so the shell
# wrapper's behaviour is identical (300s window, 5 crashes, 1s restart sleep).
SLIDING_WINDOW_SECONDS = 300.0
SLIDING_CRASH_LIMIT = 5
SLIDING_RESTART_SLEEP_SECONDS = 1.0

# ---------------------------------------------------------------------------
# systemd service co-design (modulo.launcher.service)
# ---------------------------------------------------------------------------

# How long systemd waits before restarting a failed launcher. The launcher's
# own restart budget is the in-app crash cap; this only shapes the OS-side
# restart cadence for the launcher process itself.
SERVICE_RESTART_SECONDS = 5.0

# The launcher-owned public surface (vulture's dead-code gate special-cases
# __all__).
__all__ = [
    "CRASH_CAP",
    "CRASH_WINDOW_SECONDS",
    "DEGRADED_BACKTRACE_RING",
    "DEGRADED_EXIT_CODE",
    "HEALTH_CHECK_INTERVAL",
    "HEALTH_CHECK_TIMEOUT",
    "PG_FAST_SHUTDOWN_TIMEOUT",
    "RESTART_BACKOFF_INITIAL",
    "RESTART_BACKOFF_MAX",
    "SERVICE_RESTART_SECONDS",
    "SHUTDOWN_GRACE_SECONDS",
    "SLIDING_CRASH_LIMIT",
    "SLIDING_RESTART_SLEEP_SECONDS",
    "SLIDING_WINDOW_SECONDS",
    "TEARDOWN_PRIORITY_POSTGRES",
    "TEARDOWN_PRIORITY_REDIS",
    "TEARDOWN_PRIORITY_SAQ",
    "TICK_SECONDS",
    "UPGRADE_DEGRADED_WINDOW_SECONDS",
    "format_shell_constants",
    "shell_constants",
]


def shell_constants() -> dict[str, str]:
    """The shell-sourceable form of the policy constants (``KEY=value``).

    Keys are stable names the entrypoint (and operators) rely on: the
    ``SLIDING_*`` names match the variables the container entrypoint has
    always consumed; the ``SUPERVISOR_*`` names expose the in-app
    supervisor's production defaults for observability.
    """
    return {
        "SLIDING_WINDOW_S": str(int(SLIDING_WINDOW_SECONDS)),
        "SLIDING_CRASH_LIMIT": str(int(SLIDING_CRASH_LIMIT)),
        "SLIDING_RESTART_SLEEP_S": str(int(SLIDING_RESTART_SLEEP_SECONDS)),
        "SUPERVISOR_TICK_SECONDS": repr(TICK_SECONDS),
        "SUPERVISOR_RESTART_BACKOFF_INITIAL": repr(RESTART_BACKOFF_INITIAL),
        "SUPERVISOR_RESTART_BACKOFF_MAX": repr(RESTART_BACKOFF_MAX),
        "SUPERVISOR_CRASH_WINDOW_SECONDS": repr(CRASH_WINDOW_SECONDS),
        "SUPERVISOR_CRASH_CAP": str(int(CRASH_CAP)),
        "SUPERVISOR_PG_FAST_SHUTDOWN_TIMEOUT": repr(PG_FAST_SHUTDOWN_TIMEOUT),
        "SUPERVISOR_SHUTDOWN_GRACE_SECONDS": repr(SHUTDOWN_GRACE_SECONDS),
        "DEGRADED_EXIT_CODE": str(int(DEGRADED_EXIT_CODE)),
    }


def format_shell_constants() -> str:
    """Render the ``.env``-style file the entrypoint sources (POSIX shell)."""
    lines = [
        "# Generated by `python3 -m modulo.launcher.policy` — do not edit.",
        "# Single source of supervision policy constants (FAR-674, ADR 031).",
    ]
    for key, value in shell_constants().items():
        lines.append(f"{key}={value}")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":  # pragma: no cover - thin CLI shell
    import sys

    sys.stdout.write(format_shell_constants())
