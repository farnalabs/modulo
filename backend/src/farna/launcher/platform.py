"""Platform seams for the native launcher.

The launcher deliberately keeps the service and bundled-database contract
explicit at this boundary.  POSIX implementations retain their existing
behaviour; Windows gets only decisions that are safe to make without a live
Windows host.  Windows installer, bundled Postgres/Redis, ACL, named-mutex or
Job Object validation remains an explicit integration requirement.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

__all__ = [
    "LauncherPlatformError",
    "binary_name",
    "default_data_dir",
    "initdb_locale",
    "is_windows",
    "supports_posix_file_locking",
    "supports_posix_process_groups",
    "supports_posix_security_bits",
]


class LauncherPlatformError(RuntimeError):
    """Raised when a platform-dependent launcher contract is unavailable."""


def is_windows() -> bool:
    """Return whether the current interpreter is running on Windows."""
    return sys.platform == "win32"


def default_data_dir() -> Path:
    """Return the native install's data directory for the current OS.

    Windows uses ``PROGRAMDATA`` so the bundled service and its child
    processes share one stable machine-wide data root.  The environment is
    required rather than guessed: a launcher started with a damaged Windows
    environment must fail closed instead of writing into an unexpected user
    directory.
    """
    if is_windows():
        program_data = os.environ.get("PROGRAMDATA")
        if not program_data:
            raise LauncherPlatformError("PROGRAMDATA is required for the Windows native launcher data directory")
        return Path(program_data) / "Modulo" / "data"
    xdg = os.environ.get("XDG_DATA_HOME")
    if sys.platform == "darwin":
        if xdg:
            return Path(xdg).expanduser() / "modulo" / "data"
        return Path.home() / "Library" / "Application Support" / "Modulo" / "data"
    root = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "share"
    return root / "modulo" / "data"


def binary_name(name: str) -> str:
    """Return a bundled executable name without importing Windows modules."""
    return f"{name}.exe" if is_windows() else name


def initdb_locale() -> str:
    """Return the deterministic locale accepted by bundled PostgreSQL.

    ``C.UTF-8`` is available on the supported POSIX bundles.  Windows
    PostgreSQL does not provide that POSIX locale name; ``C`` is the portable
    invariant locale and is the required Windows bootstrap choice.  A real
    Windows bundled-Postgres run must still validate the shipped binary and
    locale behaviour in the installer test environment.
    """
    return "C" if is_windows() else "C.UTF-8"


def supports_posix_file_locking() -> bool:
    """Whether POSIX ``flock`` is available for the data-dir lock."""
    return not is_windows()


def supports_posix_process_groups() -> bool:
    """Whether POSIX process-group supervision is available."""
    return not is_windows()


def supports_posix_security_bits() -> bool:
    """Whether chmod-style owner-only permission bits are meaningful."""
    return os.name == "posix" and not is_windows()
