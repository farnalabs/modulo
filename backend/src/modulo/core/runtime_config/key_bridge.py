"""Store-override bridges for hot-reloadable keys (FAR-1135).

FAR-1131 bridged ``MODULO_TELEMETRY_ENABLED`` with
``telemetry_bridge.py``; this module provides the generic helpers the
remaining hot-reloadable keys use so a runtime override
(``PUT /api/v1/admin/runtime-config``) actually reaches the governed code:

- :func:`override_or` — read the override if one is set, else fall back to
  the caller's boot-time value (a ``Settings`` field or ``os.environ``
  read). This is the FAR-1131 pattern generalised: consumers keep their
  existing fallback, the store wins only when an override exists.
- :func:`override_int_or` — integer-typed variant with safe parsing.
- :func:`apply_log_level` — an *apply-on-write* hook: logging levels are
  configured once, so the runtime-config route invokes this after a
  ``MODULO_LOG_LEVEL`` override/clear to take effect immediately (the
  pre-override root level is captured and restored on clear).

Registration: every hot-reloadable key in ``store._KEY_CONFIG`` names its
store-reading consumer(s); ``test_key_registry.py`` fails if a key is added
without one, so a new dead switch cannot ship silently.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from modulo.core.runtime_config.store import get_runtime_config_store

_log = logging.getLogger(__name__)


def get_override(key: str) -> str | None:
    """Return the runtime override for ``key``, or None when not set.

    Distinguishes "no override" (None) from an explicit empty-string
    override ("") — an empty SCIM token override, for example, must
    disable SCIM rather than fall back to the Settings value.
    """
    if not key:
        return None
    try:
        store = get_runtime_config_store()
    except RuntimeError:
        # Defensive arm mirroring telemetry_bridge: the singleton normally
        # lazily constructs and never raises; only a torn-down store lands
        # here. Any other exception signals a real bug and must propagate.
        _log.debug("RuntimeConfigStore unavailable for key %s", key)
        return None
    return store.get_override(key)


def override_or(key: str, fallback: str) -> str:
    """Return the runtime override for ``key`` when set, else ``fallback``.

    ``fallback`` is the consumer's boot-time value (Settings field or env
    read) — unchanged behaviour until an admin sets an override.
    """
    override = get_override(key)
    return fallback if override is None else override


def override_int_or(key: str, fallback: int) -> int:
    """Integer variant of :func:`override_or`.

    An unparseable override logs a warning and returns ``fallback`` — a
    bad admin input must not crash the consumer path.
    """
    override = get_override(key)
    if override is None:
        return fallback
    try:
        return int(override)
    except ValueError:
        _log.warning("Runtime config override for %s is not an integer: %r; using %d", key, override, fallback)
        return fallback


# ── apply-on-write hooks ────────────────────────────────────────────────────
# Keys whose governed state is configured once (not read per-call) register
# an apply function here; the runtime-config route invokes it after a
# set/clear so the override takes effect without a restart.

_BOOT_LOG_LEVEL: int | None = None
"""Root-logger level captured before the first override was applied."""


def apply_log_level() -> None:
    """Apply a ``MODULO_LOG_LEVEL`` override to the root logger.

    On first apply the pre-override root level is captured; clearing the
    override restores it. Called by the runtime-config PUT route (via
    ``APPLY_HOOKS``) after every ``MODULO_LOG_LEVEL`` set/clear.
    """
    global _BOOT_LOG_LEVEL

    root = logging.getLogger()
    override = get_override("MODULO_LOG_LEVEL")
    if override is None:
        if _BOOT_LOG_LEVEL is not None:
            root.setLevel(_BOOT_LOG_LEVEL)
            _BOOT_LOG_LEVEL = None
        return
    level = logging.getLevelNamesMapping().get(override.upper())
    if level is None:
        # The API validates values before storing; this arm covers direct
        # store writes. Keep the current level rather than guessing.
        _log.warning("Ignoring invalid MODULO_LOG_LEVEL override: %r", override)
        return
    if _BOOT_LOG_LEVEL is None:
        _BOOT_LOG_LEVEL = root.level
    root.setLevel(level)


APPLY_HOOKS: dict[str, Callable[[], None]] = {
    "MODULO_LOG_LEVEL": apply_log_level,
}
