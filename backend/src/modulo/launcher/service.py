"""OS service management for the single-install launcher (FAR-674, ADR 031).

Management surface for a **systemd USER unit** running ``modulo start`` in
the FOREGROUND as the installing user. Linux + systemd only: the macOS
service (launchd) is FAR-678's scope and the Windows service is a TODO(P3)
seam — both refuse loudly here.

SERVICE-MODE CONTRACT (co-design with the OS manager, locked by tests):

* **The in-app supervisor runs IDENTICALLY in service mode.** The
  supervisor owns CHILD restart — backoff, the sliding-window crash cap,
  and ordered teardown — exactly as in the foreground; the OS manager owns
  ONLY the launcher's own restart. The unit is written so the two layers
  never fight (see below).
* **Terminal degraded leaves the service STOPPED.** When the in-app crash
  cap trips, the launcher exits with :data:`policy.DEGRADED_EXIT_CODE`
  (75), which the unit lists in ``SuccessExitStatus``: systemd records the
  exit as SUCCESS, so ``Restart=on-failure`` does NOT fire and the service
  stays stopped until the operator resumes with
  ``modulo start --clear-degraded``. There is no exit code that would make
  systemd out-restart the launcher's own cap.
* **Co-designed restart budget.** The unit's ``StartLimitIntervalSec``/
  ``StartLimitBurst`` mirror :data:`policy.CRASH_WINDOW_SECONDS`/
  :data:`policy.CRASH_CAP` — systemd tolerates exactly as many launcher
  crashes inside the launcher's own window as the launcher itself
  tolerates, so neither layer oustrips the other.

Remaining facts this module encodes:

* Data dir: the user-scoped default (``~/.local/share/modulo/data`` via the
  entry's ``default_data_dir``) — machine-wide dirs are a future extension
  per ADR 031, so the unit runs plain ``modulo start`` with no override.
* Executable assumption (documented seam): the INSTALLED ``modulo`` console
  script, resolved from ``sys.argv[0]`` (a wheel/venv install whose
  console-script entry point is executing) — falling back to PATH lookup.
  Frozen/zipapp deployments would need a different resolution; not
  supported here.
* Linger: ``loginctl enable-linger`` typically requires privileges — this
  module tries ``pkexec`` first and, on failure, PRINTS the documented
  ``sudo loginctl enable-linger <user>`` fallback command instead of
  failing the install (``service status`` surfaces the linger state).
* Elevated first boot is REFUSED (ACL-poisoning guard): a user unit
  installed as root would run the bundled Postgres as root and brick the
  data dir's ownership on every later unprivileged boot.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from modulo.launcher import policy

# The published console-script name (also the unit's ExecStart program).
CONSOLE_SCRIPT_NAME = "modulo"
UNIT_FILENAME = "modulo.service"
# PID-1 systemd marker; on hosts without it, `systemctl --user` is probed.
SYSTEMD_MARKER_PATH = Path("/run/systemd/system")

CommandRunner = Callable[[list[str]], subprocess.CompletedProcess[str]]

__all__ = [
    "CONSOLE_SCRIPT_NAME",
    "UNIT_FILENAME",
    "ServiceError",
    "ServiceInstallResult",
    "ServiceUninstallResult",
    "assert_linux",
    "assert_not_elevated",
    "assert_systemd_available",
    "default_unit_path",
    "install",
    "render_unit",
    "resolve_executable",
    "resolve_username",
    "status",
    "uninstall",
]


class ServiceError(RuntimeError):
    """Raised when the service surface cannot proceed (surfaced to the CLI)."""


@dataclass
class ServiceInstallResult:
    """Outcome of ``modulo service install``."""

    unit_path: Path
    executable: Path
    enabled: bool
    started: bool
    linger_enabled: bool
    warnings: list[str] = field(default_factory=list)


@dataclass
class ServiceUninstallResult:
    """Outcome of ``modulo service uninstall`` (best-effort by design)."""

    unit_removed: bool
    stopped: bool
    disabled: bool
    linger_disabled: bool
    warnings: list[str] = field(default_factory=list)


def _default_runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
    """Run one management command (argv-only, no shell) and capture output."""
    return subprocess.run(  # noqa: S603 — argv built from module constants only
        argv,
        capture_output=True,
        text=True,
        check=False,
    )


def assert_linux() -> None:
    """Refuse non-Linux platforms loudly (macOS = FAR-678, Windows = TODO(P3))."""
    if sys.platform == "darwin":
        raise ServiceError(
            "The service install manages a systemd user unit, which macOS does not run; "
            "the macOS (launchd) service is a separate work item (FAR-678). Use "
            "`modulo start` directly meanwhile."
        )
    if sys.platform == "win32":
        # TODO(P3): Windows service seam (sc.exe / Task Scheduler).
        raise ServiceError(
            "The service install is not implemented on Windows yet (TODO(P3)); the native "
            "launcher is Linux-first (ADR 031). Use `modulo start` directly meanwhile."
        )


def assert_not_elevated() -> None:
    """Refuse an elevated install (ACL-poisoning guard, mirrors the doctor)."""
    uid = os.getuid() if hasattr(os, "getuid") else None
    if uid == 0:
        raise ServiceError(
            "refusing to install the service as root/sudo: a root-owned user unit runs the "
            "launcher and bundled Postgres as root and bricks the data dir's ownership for every "
            "later unprivileged boot — install as the unprivileged user that owns the data dir"
        )


def assert_systemd_available(runner: CommandRunner, *, marker_path: Path = SYSTEMD_MARKER_PATH) -> None:
    """Refuse non-systemd hosts, NAMING the missing manager (task contract)."""
    if marker_path.is_dir():
        return
    try:
        probe = runner(["systemctl", "--user", "is-system-running"])
    except OSError as exc:
        raise ServiceError(
            f"systemd was not detected (no {marker_path} and probing `systemctl --user` failed: "
            f"{exc}) — this command manages a systemd USER unit; on a non-systemd host run "
            "`modulo start` directly (the launcher is self-supervising)"
        ) from exc
    if probe.returncode != 0:
        raise ServiceError(
            f"systemd was not detected (no {marker_path} and `systemctl --user` did not answer) — "
            "this command manages a systemd USER unit; on a non-systemd host run `modulo start` "
            "directly (the launcher is self-supervising)"
        )


def resolve_executable(argv0: str | None = None) -> Path:
    """Resolve the INSTALLED console script (``sys.argv[0]`` assumption).

    The unit must run the installed binary, not a temp/dev path: the
    console script's own location (``sys.argv[0]`` of the running CLI) is
    the authoritative answer on a wheel/venv install; PATH lookup is the
    fallback when the CLI was invoked through some wrapper that rewrote
    argv[0].
    """
    candidate = Path(argv0 if argv0 is not None else sys.argv[0] or CONSOLE_SCRIPT_NAME)
    resolved = candidate.expanduser().resolve()
    if resolved.is_file() and os.access(resolved, os.X_OK):
        return resolved
    found = shutil.which(CONSOLE_SCRIPT_NAME)
    if found:
        return Path(found).resolve()
    raise ServiceError(
        "could not resolve the installed `modulo` executable (sys.argv[0] is not an executable "
        "file and no `modulo` is on PATH) — install the farnalabs-modulo wheel and retry"
    )


def resolve_username() -> str:
    """The installing (and service-running) user.

    ``getpass.getuser`` is the cross-platform resolver (POSIX uid → name,
    Windows env vars) — the launcher is Linux-first but this keeps the
    module importable everywhere for the test seams.
    """
    import getpass

    return str(getpass.getuser())


def default_unit_path() -> Path:
    """The systemd user-unit location for the installing user."""
    return Path.home() / ".config" / "systemd" / "user" / UNIT_FILENAME


def render_unit(executable: Path) -> str:
    """Render the user unit (pure — locked byte-for-byte by tests).

    Carries the service-mode contract in its comments: the in-app
    supervisor owns child restart, systemd owns only the launcher restart,
    the degraded exit code is listed as SUCCESS, and the restart budget
    mirrors the launcher's own crash cap (all values single-sourced from
    :mod:`modulo.launcher.policy`).
    """
    lines = [
        "[Unit]",
        "Description=Modulo single-install launcher (bundled Postgres/Redis + API)",
        "After=network-online.target",
        "Wants=network-online.target",
        "# Co-designed with the launcher's own crash cap (modulo.launcher.policy): the",
        "# in-app supervisor restarts failing children with backoff and trips its terminal",
        "# degraded state within this same window, so systemd's restart budget must not",
        "# out-shout it.",
        f"StartLimitIntervalSec={int(policy.CRASH_WINDOW_SECONDS)}s",
        f"StartLimitBurst={int(policy.CRASH_CAP)}",
        "",
        "[Service]",
        "Type=simple",
        f"ExecStart={executable} start",
        "# The in-app supervisor owns CHILD restart (backoff + crash cap + ordered",
        "# teardown); systemd owns ONLY the launcher's own restart.",
        "Restart=on-failure",
        f"RestartSec={int(policy.SERVICE_RESTART_SECONDS)}s",
        f"# Terminal degraded (crash cap exhausted) exits {int(policy.DEGRADED_EXIT_CODE)} on",
        "# purpose; listing it as success makes systemd leave the service STOPPED instead",
        "# of fighting the launcher's own cap. Resume with `modulo start --clear-degraded`.",
        f"SuccessExitStatus={int(policy.DEGRADED_EXIT_CODE)}",
        "KillSignal=SIGTERM",
        "TimeoutStopSec=90s",
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ]
    return "\n".join(lines)


def _systemctl(runner: CommandRunner, args: list[str]) -> subprocess.CompletedProcess[str]:
    return runner(["systemctl", "--user", *args])


def install(
    *,
    runner: CommandRunner | None = None,
    unit_path: Path | None = None,
    executable: Path | None = None,
    linger_argv_builder: Callable[[str], list[str]] | None = None,
) -> ServiceInstallResult:
    """Install, enable + start the user unit; enable linger (best-effort)."""
    runner = runner if runner is not None else _default_runner
    assert_linux()
    assert_not_elevated()
    assert_systemd_available(runner)
    resolved_executable = executable if executable is not None else resolve_executable()
    if not resolved_executable.is_absolute():
        raise ServiceError(f"the resolved modulo executable must be absolute for ExecStart: {resolved_executable}")
    target = unit_path if unit_path is not None else default_unit_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_unit(resolved_executable), encoding="utf-8")
    with contextlib.suppress(OSError):
        target.chmod(0o644)

    warnings: list[str] = []
    reload_result = _systemctl(runner, ["daemon-reload"])
    enable_result = _systemctl(runner, ["enable", "--now", UNIT_FILENAME])
    enabled = enable_result.returncode == 0
    if not enabled:
        detail = (enable_result.stderr or enable_result.stdout or "").strip()
        warnings.append(f"`systemctl --user enable --now` failed: {detail or enable_result.returncode}")
    linger_enabled = _enable_linger(runner, warnings, linger_argv_builder=linger_argv_builder)
    if reload_result.returncode != 0:
        detail = (reload_result.stderr or reload_result.stdout or "").strip()
        warnings.append(f"`systemctl --user daemon-reload` failed: {detail or reload_result.returncode}")
    return ServiceInstallResult(
        unit_path=target,
        executable=resolved_executable,
        enabled=enabled,
        started=enabled,  # enable --now starts the unit when enable succeeds
        linger_enabled=linger_enabled,
        warnings=warnings,
    )


def _enable_linger(
    runner: CommandRunner, warnings: list[str], *, linger_argv_builder: Callable[[str], list[str]] | None
) -> bool:
    """``loginctl enable-linger`` via pkexec; prints the sudo fallback on failure."""
    username = resolve_username()
    argv = (
        linger_argv_builder(username)
        if linger_argv_builder is not None
        else [
            "pkexec",
            "loginctl",
            "enable-linger",
            username,
        ]
    )
    try:
        result = runner(argv)
    except OSError as exc:
        warnings.append(
            f"linger could not be granted non-interactively ({exc}): run "
            f"`sudo loginctl enable-linger {username}` so the service survives logout"
        )
        return False
    if result.returncode == 0:
        return True
    detail = (result.stderr or result.stdout or "").strip()
    warnings.append(
        f"`pkexec loginctl enable-linger {username}` did not succeed ({detail or result.returncode}); "
        f"documented fallback: `sudo loginctl enable-linger {username}` (linger keeps the user "
        "service alive after logout)"
    )
    return False


def uninstall(
    *,
    runner: CommandRunner | None = None,
    unit_path: Path | None = None,
    linger_argv_builder: Callable[[str], list[str]] | None = None,
) -> ServiceUninstallResult:
    """Stop, disable, remove the unit, disable linger (best-effort, warned)."""
    runner = runner if runner is not None else _default_runner
    assert_linux()
    target = unit_path if unit_path is not None else default_unit_path()
    username = resolve_username()
    warnings: list[str] = []

    stop_result = _systemctl(runner, ["stop", UNIT_FILENAME])
    disable_result = _systemctl(runner, ["disable", UNIT_FILENAME])
    stopped = stop_result.returncode == 0
    disabled = disable_result.returncode == 0
    for name, result in (("stop", stop_result), ("disable", disable_result)):
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            warnings.append(f"`systemctl --user {name}` failed (unit may already be gone): {detail}")
    removed = False
    try:
        target.unlink(missing_ok=True)
        removed = True
    except OSError as exc:
        warnings.append(f"could not remove {target}: {exc}")
    _systemctl(runner, ["daemon-reload"])
    linger_argv = (
        linger_argv_builder(username)
        if linger_argv_builder is not None
        else [
            "pkexec",
            "loginctl",
            "disable-linger",
            username,
        ]
    )
    linger_disabled = False
    try:
        linger_result = runner(linger_argv)
        linger_disabled = linger_result.returncode == 0
    except OSError as exc:
        warnings.append(f"linger disable was not attempted ({exc}); run `sudo loginctl disable-linger {username}`")
    if not linger_disabled and not warnings:
        warnings.append(
            f"could not disable linger non-interactively — run `sudo loginctl disable-linger {username}` if wanted"
        )
    return ServiceUninstallResult(
        unit_removed=removed,
        stopped=stopped,
        disabled=disabled,
        linger_disabled=linger_disabled,
        warnings=warnings,
    )


def status(
    *,
    runner: CommandRunner | None = None,
    data_dir: Path | None = None,
) -> dict[str, Any]:
    """Unit state + linger state + launcher health (read-only)."""
    runner = runner if runner is not None else _default_runner
    assert_linux()
    from modulo.launcher.entry import default_data_dir
    from modulo.launcher.supervisor import collect_status

    target = default_unit_path()
    payload: dict[str, Any] = {"unit": UNIT_FILENAME, "unit_path": str(target)}
    enabled_result = _systemctl(runner, ["is-enabled", UNIT_FILENAME])
    payload["enabled"] = (enabled_result.stdout or "").strip() or f"exit {enabled_result.returncode}"
    active_result = _systemctl(runner, ["is-active", UNIT_FILENAME])
    payload["active"] = (active_result.stdout or "").strip() or f"exit {active_result.returncode}"
    username = resolve_username()
    linger_result = runner(["loginctl", "show-user", username, "--property=Linger"])
    linger_output = (linger_result.stdout or "").strip()
    payload["linger"] = "enabled" if "Linger=yes" in linger_output else "disabled/unknown"
    resolved_data_dir: Path | None
    try:
        resolved_data_dir = data_dir if data_dir is not None else default_data_dir()
    except RuntimeError:
        resolved_data_dir = None
    payload["launcher"] = (
        collect_status(resolved_data_dir)
        if resolved_data_dir is not None
        else {"error": "no usable default data dir on this platform"}
    )
    return payload
