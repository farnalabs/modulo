"""Runtime configuration management with provenance tracking.

Provides a process-global singleton store for configuration values with
three-tier priority: defaults < environment variables < runtime overrides.

Usage::

    from modulo.core.runtime_config import get_runtime_config_store

    store = get_runtime_config_store()
    value = store.get("DATABASE_URL")
    store.set_override("DATABASE_URL", "postgres://...")
"""

from modulo.core.runtime_config.org_flags import (
    FLAG_WORK_ITEM_AGENT_MINTING_ENABLED,
    clear_org_flag_cache,
    is_org_flag_enabled,
    read_org_flag,
    set_org_flag,
)
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
    "FLAG_WORK_ITEM_AGENT_MINTING_ENABLED",
    "HOT_RELOADABLE_KEYS",
    "KNOWN_KEYS",
    "ConfigEntry",
    "RuntimeConfigStore",
    "clear_org_flag_cache",
    "get_runtime_config_store",
    "is_org_flag_enabled",
    "read_org_flag",
    "set_org_flag",
]
