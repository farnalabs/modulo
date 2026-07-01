import logging
from functools import lru_cache

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings

_log = logging.getLogger(__name__)

_MIN_KEY_LEN = 32
# Known placeholder values that operators paste from docs without changing.
_BLOCKED_SECRET_KEYS = frozenset({"changeme", "secret", "your-secret-key", "development", "test", "insecure"})


class Settings(BaseSettings):
    # pydantic-settings v2: field names are uppercased to derive env var names,
    # so `secret_key` → SECRET_KEY automatically. No `env=` kwarg needed.
    database_url: str = Field(...)
    secret_key: str = Field(...)
    # FERNET_KEY encrypts stored connector credentials — separate from JWT secret.
    fernet_key: str = Field(...)
    # FERNET_KEY_OLD — optional previous key for no-downtime rotation period.
    # When set, decrypt operations try fernet_key first, then fall back to this.
    fernet_key_old: str = Field(default="")
    redis_url: str = Field("redis://localhost:6379/0")
    modulo_ws_token_ttl_seconds: int = Field(60)
    debug: bool = Field(False)

    # Alpha auth — at least one of these must be non-empty for login to work.
    modulo_admin_password: str = Field("")
    # Multi-user format: "user1:$2b$12$hash,user2:$2b$12$hash"
    modulo_users: str = Field("")

    modulo_public_url: str = Field("http://localhost:8000")
    modulo_demo_mode: bool = Field(False)
    modulo_license_key: str = Field("")

    # SSO / OIDC — JSON array of {provider_id, client_id, client_secret, discovery_url}
    modulo_oidc_providers: str = Field("[]")

    # SSO / SAML 2.0 (enterprise, requires license key)
    modulo_saml_enabled: bool = Field(False)
    modulo_saml_idp_metadata_url: str = Field("")
    modulo_saml_idp_metadata_xml: str = Field("")
    modulo_saml_entity_id: str = Field("modulo")
    modulo_saml_sp_private_key: str = Field("")
    modulo_saml_sp_x509_cert: str = Field("")

    # SSO defaults
    modulo_sso_default_role: str = Field("runner")

    # "postgres" (default), "sqlite", "mariadb", or "mysql" — sqlite disables RLS,
    # advisory locks, flood protection, and other Postgres-specific security features.
    # "mariadb" / "mysql" use asyncmy driver.
    modulo_db: str = Field("postgres")

    modulo_ratelimit_bypass_token: str = Field("")

    # Auth-specific rate limiting
    modulo_auth_rate_limit_enabled: bool = Field(True)
    modulo_auth_max_attempts: int = Field(10)
    modulo_auth_window_seconds: int = Field(60)

    # Inactivity timeout in minutes (default 480 = 8h). Set to 0 to disable.
    inactivity_timeout_minutes: int = Field(480)

    # Structured logging level (default: INFO). Per-module override via MODULO_LOG_LEVEL_<MODULE>.
    modulo_log_level: str = Field("INFO")

    # Comma-separated list of CORS origins. Mapped from CORS_ORIGINS env var.
    cors_origins: str = Field("http://localhost:5173")

    # Preflight cache max-age in seconds (default 600 = 10 min). Mapped from CORS_MAX_AGE.
    cors_max_age: int = Field(600)

    modulo_scim_token: str = Field("")

    # Default org ID for SCIM provisioning. If empty, the first org in the DB is used.
    modulo_scim_default_org_id: str = Field("")

    # Telemetry — disabled by default for data residency compliance.
    # Set to "true" to enable OTel stdout + OTLP exporters.
    modulo_telemetry_enabled: bool = Field(False)

    modulo_otel_service_name: str = Field("modulo")

    # SSE event stream limits
    modulo_sse_max_connections_per_org: int = Field(100)
    modulo_sse_max_connections_per_user: int = Field(10)
    modulo_sse_zombie_timeout_seconds: float = Field(2.0)

    # CSRF protection
    modulo_csrf_enabled: bool = Field(True)
    modulo_csrf_exempt_paths: str = Field("/api/v1/health,/api/v1/triggers,/api/v1/auth")

    # Plugin discovery — when enabled, scans installed packages for entry points
    # registered in the ``modulo.connectors`` and ``modulo.model_backends`` groups.
    # Set to "false" to disable plugin discovery at startup.
    modulo_plugin_discovery: bool = Field(True)

    # Secrets backend — determines how connector/backend credentials are stored.
    # Options: "fernet" (default), "vault", "aws". Env var: MODULO_SECRETS_BACKEND.
    modulo_secrets_backend: str = Field("fernet")

    # Vault configuration (used when MODULO_SECRETS_BACKEND=vault)
    vault_addr: str = Field("")
    vault_token: str = Field("")
    vault_role_id: str = Field("")
    vault_secret_id: str = Field("")

    # AWS Secrets Manager configuration (used when MODULO_SECRETS_BACKEND=aws)
    aws_access_key_id: str = Field("")
    aws_secret_access_key: str = Field("")
    aws_region: str = Field("us-east-1")
    aws_profile: str = Field("")

    model_config = {"env_file": ".env", "case_sensitive": False, "extra": "ignore"}

    @field_validator("secret_key")
    @classmethod
    def _secret_key_is_strong(cls, v: str) -> str:
        if v.lower() in _BLOCKED_SECRET_KEYS:
            raise ValueError("SECRET_KEY is a known placeholder value — generate a random key")
        if len(v.encode()) < _MIN_KEY_LEN:
            raise ValueError(f"SECRET_KEY must be at least {_MIN_KEY_LEN} bytes; got {len(v.encode())}")
        return v

    @field_validator("fernet_key")
    @classmethod
    def _fernet_key_is_strong(cls, v: str) -> str:
        if len(v.encode()) < _MIN_KEY_LEN:
            raise ValueError(f"FERNET_KEY must be at least {_MIN_KEY_LEN} bytes; got {len(v.encode())}")
        return v

    @field_validator("fernet_key_old")
    @classmethod
    def _fernet_key_old_is_strong_if_set(cls, v: str) -> str:
        if v and len(v.encode()) < _MIN_KEY_LEN:
            raise ValueError(f"FERNET_KEY_OLD must be at least {_MIN_KEY_LEN} bytes; got {len(v.encode())}")
        return v

    @field_validator("cors_origins")
    @classmethod
    def _validate_cors_origins(cls, v: str) -> str:
        origins = [o.strip() for o in v.split(",") if o.strip()]
        for origin in origins:
            if origin.endswith("/"):
                raise ValueError(f"CORS origin must not have trailing slash: {origin}")
        return v

    @field_validator("modulo_db")
    @classmethod
    def _validate_db(cls, v: str) -> str:
        if v.lower() not in ("postgres", "sqlite", "mariadb", "mysql"):
            raise ValueError(f"MODULO_DB must be 'postgres', 'sqlite', 'mariadb', or 'mysql'; got '{v}'")
        return v.lower()

    @model_validator(mode="after")
    def _warn_if_no_auth(self) -> "Settings":
        if not self.modulo_admin_password and not self.modulo_users:
            _log.warning("settings.no_auth_configured")
        return self

    @model_validator(mode="after")
    def _warn_deprecated_modulo_oidc_providers(self) -> "Settings":
        if self.modulo_oidc_providers and self.modulo_oidc_providers != "[]":
            _log.warning(
                "settings.deprecated_oidc_env_var — MODULO_OIDC_PROVIDERS is deprecated. "
                "Use the admin SSO providers UI at /admin/settings/sso instead.",
            )
        return self

    @model_validator(mode="after")
    def _check_cors_wildcard_in_production(self) -> "Settings":
        if not self.debug:
            origins = [o.strip() for o in self.cors_origins.split(",") if o.strip()]
            if "*" in origins:
                raise ValueError("Wildcard CORS origin (*) is not allowed when debug=False")
        return self

    @model_validator(mode="after")
    def _fix_database_url(self) -> "Settings":
        url = self.database_url
        if url.startswith("postgres://"):
            url = "postgresql+asyncpg://" + url[len("postgres://"):]
        url = url.replace("?sslmode=disable", "?ssl=disable")
        if url != self.database_url:
            self.database_url = url
            _log.info("settings.database_url_fixed")
        return self

    @model_validator(mode="after")
    def _apply_sqlite_mode(self) -> "Settings":
        if self.modulo_db.lower() == "sqlite":
            _log.warning("settings.sqlite_mode")
            if self.database_url.startswith("postgresql+asyncpg://"):
                self.database_url = "sqlite+aiosqlite:///./modulo.db"
                _log.info("settings.database_url_auto_set", extra={"database_url": self.database_url})
        elif self.modulo_db.lower() in ("mariadb", "mysql"):
            _log.warning("settings.mariadb_mode")
            if self.database_url.startswith("postgresql+asyncpg://"):
                self.database_url = "mysql+asyncmy://modulo:modulo@localhost:5435/modulo"
                _log.info("settings.database_url_auto_set", extra={"database_url": self.database_url})
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
