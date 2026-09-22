"""Runtime configuration management with provenance tracking.

Provides a process-global singleton store for configuration values with
three-tier priority: defaults < environment variables < runtime overrides.

Usage::

    from modulo.core.runtime_config import get_runtime_config_store

    store = get_runtime_config_store()
    value = store.get("DATABASE_URL")
    store.set_override("DATABASE_URL", "postgres://...")
"""

from modulo.core.runtime_config.key_bridge import (
    APPLY_HOOKS,
    apply_log_level,
    get_override,
    override_int_or,
    override_or,
)
from modulo.core.runtime_config.org_flags import (
    FLAG_COMMUNITY_OBJECTS_ENABLED,
    FLAG_WORK_ITEM_AGENT_MINTING_ENABLED,
    clear_org_flag_cache,
    is_org_flag_enabled,
    read_org_flag,
    set_org_flag,
)
from modulo.core.runtime_config.store import (
    BOOT_ONLY_REASONS,
    DEFAULT_VALUES,
    HOT_RELOADABLE_KEYS,
    KEY_CONSUMERS,
    KNOWN_KEYS,
    ConfigEntry,
    RuntimeConfigStore,
    get_runtime_config_store,
)
from modulo.core.runtime_config.telemetry_bridge import is_telemetry_enabled, toggle_telemetry

__all__ = [
    "APPLY_HOOKS",
    "BOOT_ONLY_REASONS",
    "DEFAULT_VALUES",
    "FLAG_COMMUNITY_OBJECTS_ENABLED",
    "FLAG_WORK_ITEM_AGENT_MINTING_ENABLED",
    "HOT_RELOADABLE_KEYS",
    "KEY_CONSUMERS",
    "KNOWN_KEYS",
    "ConfigEntry",
    "RuntimeConfigStore",
    "apply_log_level",
    "clear_org_flag_cache",
    "get_override",
    "get_runtime_config_store",
    "is_org_flag_enabled",
    "is_telemetry_enabled",
    "override_int_or",
    "override_or",
    "read_org_flag",
    "set_org_flag",
    "toggle_telemetry",
]
