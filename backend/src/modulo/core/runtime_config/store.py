"""Process-global singleton for runtime configuration with provenance tracking."""

from __future__ import annotations

import os
from dataclasses import dataclass

HOT_RELOADABLE_KEYS: frozenset[str] = frozenset({
    "MODULO_MAX_LOCAL_CONCURRENCY",
    "MODULO_E2B_API_KEY",
    "MODULO_LOG_LEVEL",
    "MODULO_DEMO_MODE",
    "MODULO_PLUGIN_DISCOVERY",
    "MODULO_TELEMETRY_ENABLED",
    "MODULO_OTEL_SERVICE_NAME",
    "MODULO_PUBLIC_URL",
    "MODULO_SCIM_TOKEN",
    "MODULO_SCIM_DEFAULT_ORG_ID",
    "MODULO_RATELIMIT_BYPASS_TOKEN",
    "MODULO_INACTIVITY_TIMEOUT_MINUTES",
    "DEBUG",
})

KNOWN_KEYS: list[str] = [
    "DATABASE_URL", "SECRET_KEY", "FERNET_KEY", "FERNET_KEY_OLD", "REDIS_URL",
    "MODULO_DB", "MODULO_SECRETS_BACKEND", "CORS_ORIGINS",
    "CORS_MAX_AGE", "MODULO_USERS", "MODULO_ADMIN_PASSWORD",
    "MODULO_PUBLIC_URL", "MODULO_DEMO_MODE", "MODULO_LICENSE_KEY",
    "MODULO_OIDC_PROVIDERS", "MODULO_SAML_ENABLED", "MODULO_SAML_IDP_METADATA_URL",
    "MODULO_SAML_IDP_METADATA_XML", "MODULO_SAML_ENTITY_ID",
    "MODULO_SAML_SP_PRIVATE_KEY", "MODULO_SAML_SP_X509_CERT",
    "MODULO_SSO_DEFAULT_ROLE", "MODULO_TELEMETRY_ENABLED",
    "MODULO_OTEL_SERVICE_NAME", "MODULO_PLUGIN_DISCOVERY",
    "MODULO_LOG_LEVEL", "MODULO_MAX_LOCAL_CONCURRENCY",
    "MODULO_E2B_API_KEY", "MODULO_RATELIMIT_BYPASS_TOKEN",
    "MODULO_INACTIVITY_TIMEOUT_MINUTES", "DEBUG",
    "VAULT_ADDR", "VAULT_TOKEN", "VAULT_ROLE_ID", "VAULT_SECRET_ID",
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_REGION",
    "MODULO_SCIM_TOKEN", "MODULO_SCIM_DEFAULT_ORG_ID",
]

DEFAULT_VALUES: dict[str, str] = {
    "REDIS_URL": "redis://localhost:6379/0",
    "MODULO_DB": "postgres",
    "MODULO_SECRETS_BACKEND": "fernet",
    "MODULO_PUBLIC_URL": "http://localhost:8000",
    "MODULO_DEMO_MODE": "false",
    "MODULO_PLUGIN_DISCOVERY": "true",
    "MODULO_TELEMETRY_ENABLED": "false",
    "MODULO_OTEL_SERVICE_NAME": "modulo",
    "MODULO_LOG_LEVEL": "INFO",
    "MODULO_MAX_LOCAL_CONCURRENCY": "2",
    "MODULO_SSO_DEFAULT_ROLE": "runner",
    "MODULO_SCIM_TOKEN": "",
    "MODULO_SCIM_DEFAULT_ORG_ID": "",
    "CORS_MAX_AGE": "600",
    "CORS_ORIGINS": "http://localhost:5173",
    "MODULO_RATELIMIT_BYPASS_TOKEN": "",
    "MODULO_INACTIVITY_TIMEOUT_MINUTES": "480",
    "DEBUG": "false",
    "VAULT_ADDR": "", "VAULT_TOKEN": "", "VAULT_ROLE_ID": "", "VAULT_SECRET_ID": "",
    "AWS_ACCESS_KEY_ID": "", "AWS_SECRET_ACCESS_KEY": "", "AWS_REGION": "us-east-1",
    "MODULO_USERS": "", "MODULO_ADMIN_PASSWORD": "", "MODULO_OIDC_PROVIDERS": "[]",
    "MODULO_SAML_ENABLED": "false", "MODULO_SAML_IDP_METADATA_URL": "",
    "MODULO_SAML_IDP_METADATA_XML": "", "MODULO_SAML_ENTITY_ID": "modulo",
    "MODULO_SAML_SP_PRIVATE_KEY": "", "MODULO_SAML_SP_X509_CERT": "",
    "MODULO_LICENSE_KEY": "",
}  # nosec B105 — empty-string placeholders, not hardcoded secrets


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
        self._env_values: dict[str, str | None] = {}
        self._overrides: dict[str, str] = {}

        for key in KNOWN_KEYS:
            self._defaults[key] = DEFAULT_VALUES.get(key)
            self._env_values[key] = os.environ.get(key)

    def get(self, key: str) -> str | None:
        """Return the effective value: override > env > default."""
        if key in self._overrides:
            return self._overrides[key]
        env_val = self._env_values.get(key)
        if env_val is not None:
            return env_val
        return self._defaults.get(key)

    def set_override(self, key: str, value: str) -> None:
        """Set a runtime override that stays in memory until cleared or reloaded."""
        self._overrides[key] = value

    def clear_override(self, key: str) -> None:
        """Remove a runtime override for a single key."""
        self._overrides.pop(key, None)

    def clear_all_overrides(self) -> None:
        """Remove all runtime overrides."""
        self._overrides.clear()

    def reload(self) -> None:
        """Re-read os.environ to detect drift for all known keys."""
        for key in KNOWN_KEYS:
            self._env_values[key] = os.environ.get(key)

    def get_all(self) -> list[ConfigEntry]:
        """Return all known config entries with current values and provenance."""
        items: list[ConfigEntry] = []
        for key in KNOWN_KEYS:
            default_value: str | None = self._defaults.get(key)
            env_value: str | None = self._env_values.get(key)
            override_value: str | None = self._overrides.get(key)

            if override_value is not None:
                current_value: str | None = override_value
                provenance = "override"
            elif env_value is not None:
                current_value = env_value
                provenance = "environment"
            else:
                current_value = default_value
                provenance = "default"

            items.append(ConfigEntry(
                key=key,
                current_value=current_value,
                default_value=default_value,
                env_value=env_value,
                override_value=override_value,
                provenance=provenance,
                hot_reloadable=key in HOT_RELOADABLE_KEYS,
            ))
        return items


_store: RuntimeConfigStore | None = None


def get_runtime_config_store() -> RuntimeConfigStore:
    """Return the process-global RuntimeConfigStore singleton."""
    global _store
    if _store is None:
        _store = RuntimeConfigStore()
    return _store
