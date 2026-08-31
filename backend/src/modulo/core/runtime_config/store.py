"""Process-global singleton for runtime configuration with provenance tracking."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from threading import Lock, RLock

from modulo.util import sanitise_log_value as _sanitise_log_value

_log = logging.getLogger(__name__)


@dataclass
class _KeyConfig:
    default: str | None = None
    hot_reloadable: bool = False


_KEY_CONFIG: dict[str, _KeyConfig] = {
    "DATABASE_URL": _KeyConfig(),
    "SECRET_KEY": _KeyConfig(),
    "FERNET_KEY": _KeyConfig(),
    "FERNET_KEY_OLD": _KeyConfig(),
    "REDIS_URL": _KeyConfig(default="redis://localhost:6379/0"),
    "MODULO_DB": _KeyConfig(default="postgres"),
    "MODULO_SECRETS_BACKEND": _KeyConfig(default="fernet"),
    "CORS_ORIGINS": _KeyConfig(default="http://localhost:5173"),
    "CORS_MAX_AGE": _KeyConfig(default="600"),
    "MODULO_USERS": _KeyConfig(default=""),
    "MODULO_ADMIN_PASSWORD": _KeyConfig(default=""),
    "MODULO_PUBLIC_URL": _KeyConfig(default="http://localhost:8000", hot_reloadable=True),
    "MODULO_LICENSE_KEY": _KeyConfig(default=""),
    "MODULO_OIDC_PROVIDERS": _KeyConfig(default="[]"),
    "MODULO_SAML_ENABLED": _KeyConfig(default="false"),
    "MODULO_SAML_IDP_METADATA_URL": _KeyConfig(default=""),
    "MODULO_SAML_IDP_METADATA_XML": _KeyConfig(default=""),
    "MODULO_SAML_ENTITY_ID": _KeyConfig(default="modulo"),
    "MODULO_SAML_SP_PRIVATE_KEY": _KeyConfig(default=""),
    "MODULO_SAML_SP_X509_CERT": _KeyConfig(default=""),
    "MODULO_SSO_DEFAULT_ROLE": _KeyConfig(default="runner"),
    "MODULO_TELEMETRY_ENABLED": _KeyConfig(default="false", hot_reloadable=True),
    "MODULO_OTEL_SERVICE_NAME": _KeyConfig(default="modulo", hot_reloadable=True),
    "MODULO_PLUGIN_DISCOVERY": _KeyConfig(default="true", hot_reloadable=True),
    "MODULO_LOG_LEVEL": _KeyConfig(default="INFO", hot_reloadable=True),
    "MODULO_MAX_LOCAL_CONCURRENCY": _KeyConfig(default="2", hot_reloadable=True),
    "MODULO_E2B_API_KEY": _KeyConfig(hot_reloadable=True),
    "MODULO_RATELIMIT_BYPASS_TOKEN": _KeyConfig(default="", hot_reloadable=True),
    "MODULO_INACTIVITY_TIMEOUT_MINUTES": _KeyConfig(default="480", hot_reloadable=True),
    "DEBUG": _KeyConfig(default="false", hot_reloadable=True),
    "VAULT_ADDR": _KeyConfig(default=""),
    "VAULT_TOKEN": _KeyConfig(default=""),
    "VAULT_ROLE_ID": _KeyConfig(default=""),
    "VAULT_SECRET_ID": _KeyConfig(default=""),
    "AWS_ACCESS_KEY_ID": _KeyConfig(default=""),
    "AWS_SECRET_ACCESS_KEY": _KeyConfig(default=""),
    "AWS_REGION": _KeyConfig(default="us-east-1"),
    "MODULO_SCIM_TOKEN": _KeyConfig(default="", hot_reloadable=True),
    "MODULO_SCIM_DEFAULT_ORG_ID": _KeyConfig(default="", hot_reloadable=True),
}

KNOWN_KEYS: tuple[str, ...] = tuple(_KEY_CONFIG.keys())
HOT_RELOADABLE_KEYS: frozenset[str] = frozenset(k for k, v in _KEY_CONFIG.items() if v.hot_reloadable)
DEFAULT_VALUES: dict[str, str] = {k: v.default for k, v in _KEY_CONFIG.items() if v.default is not None}


@dataclass
class ConfigEntry:
    key: str
    current_value: str | None
    default_value: str | None
    env_value: str | None
    override_value: str | None
    provenance: str
    hot_reloadable: bool


class RuntimeConfigStore:
    """Process-global store tracking config values with provenance.

    Three tiers: defaults (hardcoded) < env (from os.environ) < overrides (runtime API).
    """

    def __init__(self) -> None:
        self._defaults: dict[str, str | None] = {}
        self._overrides: dict[str, str | None] = {}
        self._env_values: dict[str, str | None] = {}
        self._lock = RLock()

        self._defaults = {key: DEFAULT_VALUES.get(key) for key in KNOWN_KEYS}
        self._refresh_env_values()

    @classmethod
    def reset(cls) -> None:
        """Reset the module-level singleton (for test isolation)."""
        global _store

        _store = None

    def _resolve(self, key: str) -> tuple[str | None, str]:
        """Resolve effective value and provenance for a key.

        Returns (value, provenance) with override > env > default priority.
        """
        with self._lock:
            override_val = self._overrides.get(key)
            if override_val is not None:
                return override_val, "override"
            env_val = self._env_values.get(key)
            if env_val is not None:
                return env_val, "environment"
            return self._defaults.get(key), "default"

    def get(self, key: str) -> str | None:
        """Return the effective value: override > env > default."""
        if not key:
            return None
        value, _ = self._resolve(key)
        return value

    def set_override(self, key: str, value: str) -> None:
        """Set a runtime override that stays in memory until cleared or reloaded."""
        if not key or key.strip() != key:
            _log.warning("Runtime config override rejected: invalid key %r", _sanitise_log_value(key))
            return
        if key not in KNOWN_KEYS:
            _log.warning("Runtime config override set for unknown key: %s", _sanitise_log_value(key))
        with self._lock:
            self._overrides[key] = value
            _log.info("Runtime config override set: %s", _sanitise_log_value(key))

    def clear_override(self, key: str) -> None:
        """Remove a runtime override for a single key."""
        if not key or key.strip() != key:
            _log.warning("Runtime config override clear rejected: invalid key %r", _sanitise_log_value(key))
            return
        with self._lock:
            removed = self._overrides.pop(key, None)
            if removed is not None:
                _log.info("Runtime config override cleared: %s", _sanitise_log_value(key))
            else:
                _log.debug("Runtime config override not found (no-op): %s", _sanitise_log_value(key))

    def clear_all_overrides(self) -> None:
        """Remove all runtime overrides."""
        with self._lock:
            self._overrides.clear()
            _log.info("Runtime config all overrides cleared")

    def _refresh_env_values(self) -> None:
        """Read all known keys from the process environment."""
        self._env_values = {key: os.environ.get(key) for key in KNOWN_KEYS}

    def reload(self) -> None:
        """Re-read os.environ to detect drift for all known keys."""
        with self._lock:
            self._refresh_env_values()
        _log.info("Runtime config reloaded from environment")

    def get_all(self) -> list[ConfigEntry]:
        """Return all known config entries with current values and provenance."""
        with self._lock:
            items: list[ConfigEntry] = []
            for key in KNOWN_KEYS:
                default_value: str | None = self._defaults.get(key)
                env_value: str | None = self._env_values.get(key)
                override_value: str | None = self._overrides.get(key)
                current_value, provenance = self._resolve(key)

                items.append(
                    ConfigEntry(
                        key=key,
                        current_value=current_value,
                        default_value=default_value,
                        env_value=env_value,
                        override_value=override_value,
                        provenance=provenance,
                        hot_reloadable=key in HOT_RELOADABLE_KEYS,
                    )
                )
        return items


_store: RuntimeConfigStore | None = None
_store_lock: Lock = Lock()


def get_runtime_config_store() -> RuntimeConfigStore:
    """Return the process-global RuntimeConfigStore singleton."""
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = RuntimeConfigStore()
    return _store
