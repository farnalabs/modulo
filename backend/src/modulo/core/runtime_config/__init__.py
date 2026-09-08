"""Runtime configuration management with provenance tracking.

Provides a process-global singleton store for configuration values with
three-tier priority: defaults < environment variables < runtime overrides.

Usage::

    from modulo.core.runtime_config import get_runtime_config_store

    store = get_runtime_config_store()
    value = store.get("DATABASE_URL")
    store.set_override("DATABASE_URL", "postgres://...")
"""

from modulo.core.runtime_config.store import (
    DEFAULT_VALUES,
    HOT_RELOADABLE_KEYS,
    KNOWN_KEYS,
    ConfigEntry,
    RuntimeConfigStore,
    get_runtime_config_store,
)

__all__ = [
    "DEFAULT_VALUES",
    "HOT_RELOADABLE_KEYS",
    "KNOWN_KEYS",
    "ConfigEntry",
    "RuntimeConfigStore",
    "get_runtime_config_store",
]
