"""Process-global singleton for runtime configuration with provenance tracking."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from threading import Lock, RLock

from modulo.util import sanitise_log_value as _sanitise_log_value

_log = logging.getLogger(__name__)


# Boot-only reasons (shared, kept short — the API surfaces these verbatim
# when an override for a boot-only key is rejected).
_REASON_ENV_BOOT = "read from the environment via Settings at process start; set the env var and restart"
_REASON_INFRA = "infrastructure connection setting applied at process start; restart to change"
_REASON_SECRET = "secret material fixed at process start; rotate via the environment and restart"
_REASON_STARTUP = "applied during application startup; restart to change"


@dataclass
class _KeyConfig:
    default: str | None = None
    hot_reloadable: bool = False
    # Store-reading consumer(s) ("module:function") that make a runtime
    # override take effect. Required iff hot_reloadable=True — enforced by
    # tests/unit/core/runtime_config/test_key_registry.py so a new hot key
    # cannot ship without a real reader (FAR-1135).
    consumers: tuple[str, ...] = ()
    # Why a runtime override cannot take effect. Required iff
    # hot_reloadable=False — the same guard test fails an unclassified key.
    boot_reason: str | None = None


_KEY_CONFIG: dict[str, _KeyConfig] = {
    # ── Boot-only: infrastructure / secrets / startup-applied ────────────────
    "DATABASE_URL": _KeyConfig(boot_reason=_REASON_INFRA),
    "SECRET_KEY": _KeyConfig(boot_reason=_REASON_SECRET),
    "FERNET_KEY": _KeyConfig(boot_reason=_REASON_SECRET),
    "FERNET_KEY_OLD": _KeyConfig(boot_reason=_REASON_SECRET),
    "REDIS_URL": _KeyConfig(default="redis://localhost:6379/0", boot_reason=_REASON_INFRA),
    "MODULO_DB": _KeyConfig(default="postgres", boot_reason=_REASON_INFRA),
    "MODULO_SECRETS_BACKEND": _KeyConfig(default="fernet", boot_reason=_REASON_ENV_BOOT),
    "CORS_ORIGINS": _KeyConfig(default="http://localhost:5173", boot_reason=_REASON_STARTUP),
    "CORS_MAX_AGE": _KeyConfig(default="600", boot_reason=_REASON_STARTUP),
    "MODULO_USERS": _KeyConfig(default="", boot_reason=_REASON_STARTUP),
    "MODULO_ADMIN_PASSWORD": _KeyConfig(default="", boot_reason=_REASON_STARTUP),
    # Deferred hot-bridge (FAR-1135): read via get_settings() across many
    # request handlers — not safe to bridge in one focused change.
    "MODULO_PUBLIC_URL": _KeyConfig(
        default="http://localhost:8000",
        boot_reason=(
            "read via Settings across request handlers; hot-bridge deferred "
            "(FAR-1135 follow-up) — set MODULO_PUBLIC_URL and restart"
        ),
    ),
    "MODULO_LICENSE_KEY": _KeyConfig(default="", boot_reason=_REASON_ENV_BOOT),
    "MODULO_OIDC_PROVIDERS": _KeyConfig(default="[]", boot_reason=_REASON_STARTUP),
    "MODULO_SAML_ENABLED": _KeyConfig(default="false", boot_reason=_REASON_STARTUP),
    "MODULO_SAML_IDP_METADATA_URL": _KeyConfig(default="", boot_reason=_REASON_STARTUP),
    "MODULO_SAML_IDP_METADATA_XML": _KeyConfig(default="", boot_reason=_REASON_STARTUP),
    "MODULO_SAML_ENTITY_ID": _KeyConfig(default="modulo", boot_reason=_REASON_STARTUP),
    "MODULO_SAML_SP_PRIVATE_KEY": _KeyConfig(default="", boot_reason=_REASON_SECRET),
    "MODULO_SAML_SP_X509_CERT": _KeyConfig(default="", boot_reason=_REASON_STARTUP),
    "MODULO_SSO_DEFAULT_ROLE": _KeyConfig(default="runner", boot_reason=_REASON_STARTUP),
    # ── Hot-reloadable: every entry names its store-reading consumer(s) ──────
    "MODULO_TELEMETRY_ENABLED": _KeyConfig(
        default="false",
        hot_reloadable=True,
        consumers=("modulo.core.runtime_config.telemetry_bridge:is_telemetry_enabled",),
    ),
    "MODULO_OTEL_SERVICE_NAME": _KeyConfig(
        default="modulo",
        boot_reason=(
            "applied when the OTel pipeline is configured at startup; "
            "restart (or re-run the telemetry toggle) to re-apply"
        ),
    ),
    "MODULO_PLUGIN_DISCOVERY": _KeyConfig(default="true", boot_reason=_REASON_STARTUP),
    "MODULO_LOG_LEVEL": _KeyConfig(
        default="INFO",
        hot_reloadable=True,
        consumers=("modulo.core.runtime_config.key_bridge:apply_log_level",),
    ),
    "MODULO_MAX_LOCAL_CONCURRENCY": _KeyConfig(
        default="2",
        hot_reloadable=True,
        consumers=(
            "modulo.api.routes.environment_profiles:_get_hub",
            "modulo.core.bundled_runner.runner_dispatch:resolve_sandbox_dispatch_route",
            "modulo.core.runtime_provider.local:create_local_provider_from_env",
        ),
    ),
    # Credential: rotating the E2B key touches the provider registration gate
    # and the node-runner enforcement check — bridged only with both, deferred.
    "MODULO_E2B_API_KEY": _KeyConfig(
        boot_reason=(
            "credential resolved when the E2B provider registers; hot-bridge "
            "deferred (FAR-1135 follow-up) — set the env var and restart"
        ),
    ),
    "MODULO_RATELIMIT_BYPASS_TOKEN": _KeyConfig(
        default="",
        hot_reloadable=True,
        consumers=(
            "modulo.api.middleware.rate_limiter:RateLimitMiddleware._should_rate_limit",
            "modulo.api.middleware.rate_limiter:AuthRateLimitMiddleware._should_rate_limit",
        ),
    ),
    # No consumer exists anywhere in the codebase (not even Settings is read).
    "MODULO_INACTIVITY_TIMEOUT_MINUTES": _KeyConfig(
        default="480",
        boot_reason="no consumer reads this setting (FAR-1135 follow-up: wire a consumer or remove the key)",
    ),
    # Security-sensitive: flipping DEBUG at runtime would make session cookies
    # drop their Secure flag — fixed at process start on purpose.
    "DEBUG": _KeyConfig(
        default="false",
        boot_reason="security-sensitive: cookie Secure flags and security headers are fixed at process start",
    ),
    "VAULT_ADDR": _KeyConfig(default="", boot_reason=_REASON_ENV_BOOT),
    "VAULT_TOKEN": _KeyConfig(default="", boot_reason=_REASON_SECRET),
    "VAULT_ROLE_ID": _KeyConfig(default="", boot_reason=_REASON_SECRET),
    "VAULT_SECRET_ID": _KeyConfig(default="", boot_reason=_REASON_SECRET),
    "AWS_ACCESS_KEY_ID": _KeyConfig(default="", boot_reason=_REASON_SECRET),
    "AWS_SECRET_ACCESS_KEY": _KeyConfig(default="", boot_reason=_REASON_SECRET),
    "AWS_REGION": _KeyConfig(default="us-east-1", boot_reason=_REASON_ENV_BOOT),
    "MODULO_SCIM_TOKEN": _KeyConfig(
        default="",
        hot_reloadable=True,
        consumers=("modulo.auth.scim_auth:get_scim_principal",),
    ),
    "MODULO_SCIM_DEFAULT_ORG_ID": _KeyConfig(
        default="",
        hot_reloadable=True,
        consumers=("modulo.auth.scim_auth:get_scim_principal",),
    ),
}

KNOWN_KEYS: tuple[str, ...] = tuple(_KEY_CONFIG.keys())
HOT_RELOADABLE_KEYS: frozenset[str] = frozenset(k for k, v in _KEY_CONFIG.items() if v.hot_reloadable)
DEFAULT_VALUES: dict[str, str] = {k: v.default for k, v in _KEY_CONFIG.items() if v.default is not None}
# Classification views used by the API (boot-only rejection) and the guard
# test: every KNOWN_KEY appears in exactly one of these two dicts.
KEY_CONSUMERS: dict[str, tuple[str, ...]] = {k: v.consumers for k, v in _KEY_CONFIG.items() if v.consumers}
BOOT_ONLY_REASONS: dict[str, str] = {k: v.boot_reason for k, v in _KEY_CONFIG.items() if v.boot_reason}


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
        """Set a runtime override that stays in memory until cleared or reloaded.

        Note: the store is the mechanism; the admin API
        (``PUT /api/v1/admin/runtime-config``) is the policy layer and
        rejects overrides for keys classified boot-only in
        ``BOOT_ONLY_REASONS`` (FAR-1135).
        """
        if not key or key.strip() != key:
            _log.warning("Runtime config override rejected: invalid key %r", _sanitise_log_value(key))
            return
        if key not in KNOWN_KEYS:
            _log.warning("Runtime config override set for unknown key: %s", _sanitise_log_value(key))
        with self._lock:
            self._overrides[key] = value
            _log.info("Runtime config override set: %s", _sanitise_log_value(key))

    def get_override(self, key: str) -> str | None:
        """Return the raw runtime override for ``key``, or None when unset.

        Unlike :meth:`get`, this exposes only the override tier — bridge
        helpers (:mod:`modulo.core.runtime_config.key_bridge`) use it to
        layer an override on top of a consumer's boot-time Settings/env
        value without masking that value with the store's default tier.
        """
        if not key:
            return None
        with self._lock:
            return self._overrides.get(key)

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
