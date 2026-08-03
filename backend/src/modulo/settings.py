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
    modulo_dogfood_enabled: bool = Field(False)
    modulo_license_key: str = Field("")
    # Ed25519 public key (hex) for license signature verification.
    # Defaults to dev/test key — set MODULO_LICENSE_PUBLIC_KEY in production.
    modulo_license_public_key: str = Field("")

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
    # "mariadb" / "mysql" use the aiomysql driver.
    modulo_db: str = Field("postgres")

    modulo_ratelimit_bypass_token: str = Field("")

    modulo_max_local_concurrency: int = Field(2)

    # ------------------------------------------------------------------
    # SAQ (Celery removed in PR C) — plan F4 Settings section
    # ------------------------------------------------------------------
    # SAQ is the ONLY dispatch path — dispatch_run always enqueues to SAQ and
    # the dispatcher column always reads 'saq' (plan F3e). No enable flag.
    # SAQ runs queue name — 'runs' or 'staging-runs' (staging isolation, plan F1).
    saq_runs_queue: str = Field(default="runs", alias="SAQ_RUNS_QUEUE")
    # Staleness gate for re-claiming a SAQ run whose heartbeat is stale.
    run_claim_stale_seconds: int = Field(default=450, alias="RUN_CLAIM_STALE_SECONDS", ge=1, le=3600)
    # Legacy claim window kept for the shared sync claim helpers (claim_run /
    # execute_run legacy path), which the SAQ worker does not use.
    legacy_run_claim_stale_seconds: int = Field(default=180, alias="LEGACY_RUN_CLAIM_STALE_SECONDS", ge=1, le=3600)
    # SAQ job heartbeat knob (per-job). The DB heartbeat cadence is
    # RUN_HEARTBEAT_SECONDS below.
    saq_job_heartbeat: int = Field(default=300, alias="SAQ_JOB_HEARTBEAT", ge=1, le=3600)
    # DB heartbeat cadence for execute_run — keep well below the 300s SAQ sweep
    # threshold (cadence equal to the sweep threshold leaves zero margin).
    run_heartbeat_seconds: int = Field(default=30, alias="RUN_HEARTBEAT_SECONDS", ge=1, le=120)
    # Cutover hold gate: healthz/ready 503-gates when THIS machine's SAQ workers
    # are stale (default true during the hold). Set false via deploy-time flag
    # after the hold to relax the gate to degraded (alerting continues).
    saq_hard_gate: bool = Field(default=True, alias="SAQ_HARD_GATE")
    # Web UI auth — FAIL-CLOSED (system worker refuses to boot without both).
    saq_auth_password: str | None = Field(default=None, alias="SAQ_AUTH_PASSWORD", repr=False)
    saq_auth_username: str | None = Field(default=None, alias="SAQ_AUTH_USERNAME")
    # SAQ retry knobs — retries=N is N TOTAL attempts (N-1 retries).
    saq_run_retries: int = Field(default=5, alias="SAQ_RUN_RETRIES", ge=1, le=20)
    # Deterministic fixed delay (retry_backoff=False).
    saq_retry_delay: int = Field(default=60, alias="SAQ_RETRY_DELAY", ge=1, le=3600)
    # Per-claim E2B idempotency key run:{id}:e2b:{claim_token} (F3a).
    saq_e2b_idempotency: bool = Field(default=True, alias="SAQ_E2B_IDEMPOTENCY")
    # TEST-ONLY pause flag — hard default off; refused outside test/staging
    # (debug=false).
    saq_test_pause: bool = Field(default=False, alias="SAQ_TEST_PAUSE")
    # Legacy sweep windows (never_dispatched / worker_lost / re-enqueue) match
    # today's beat-sweep values (5 min / 10 min; re-enqueue is SAQ-only, 600 is
    # unverifiable from today's Celery code), decoupled from
    # RUN_CLAIM_STALE_SECONDS (SAQ only).
    saq_reenqueue_window: int = Field(default=600, alias="SAQ_REENQUEUE_WINDOW", ge=1, le=3600)
    saq_never_dispatched_window: int = Field(default=300, alias="SAQ_NEVER_DISPATCHED_WINDOW", ge=1, le=3600)
    saq_worker_lost_window: int = Field(default=600, alias="SAQ_WORKER_LOST_WINDOW", ge=1, le=3600)
    # SAQ worker DB pool size (per worker; Postgres budget — F4).
    saq_worker_db_pool_size: int = Field(default=10, alias="SAQ_WORKER_DB_POOL_SIZE", ge=1, le=10)
    # SAQ Redis client pool size (Upstash connection budget — F2).
    saq_redis_pool_size: int = Field(default=5, alias="SAQ_REDIS_POOL_SIZE", ge=1, le=50)
    # Per-run agent runtime cost: E2B sandbox hourly rate used to estimate
    # sandbox_agent node cost from wall-clock time (E2B bills per-second
    # sandbox uptime). Default reflects the dashboard-confirmed opencode
    # template = 2 vCPU / 2 GiB at E2B per-second rates (~$0.133/hr).
    e2b_sandbox_usd_per_hour: float = Field(default=0.13, alias="E2B_SANDBOX_USD_PER_HOUR", ge=0)

    # ------------------------------------------------------------------
    # Health check timeouts (seconds) — configurable per-check limits for
    # /healthz/ready dependency probes. Each per-check override defaults to
    # 0, meaning "fall back to MODULO_HEALTH_TIMEOUT_SECONDS". Overrides are
    # capped at 60s so a single hung dependency cannot stall readiness.
    # ------------------------------------------------------------------
    modulo_health_timeout_seconds: float = Field(default=5.0, alias="MODULO_HEALTH_TIMEOUT_SECONDS", ge=0.5, le=60)
    modulo_health_db_timeout_seconds: float = Field(
        default=0.0, alias="MODULO_HEALTH_DB_TIMEOUT_SECONDS", ge=0.0, le=60
    )
    modulo_health_redis_timeout_seconds: float = Field(
        default=0.0, alias="MODULO_HEALTH_REDIS_TIMEOUT_SECONDS", ge=0.0, le=60
    )
    modulo_health_checkpointer_timeout_seconds: float = Field(
        default=0.0, alias="MODULO_HEALTH_CHECKPOINTER_TIMEOUT_SECONDS", ge=0.0, le=60
    )
    modulo_health_migrations_timeout_seconds: float = Field(
        default=0.0, alias="MODULO_HEALTH_MIGRATIONS_TIMEOUT_SECONDS", ge=0.0, le=60
    )

    # Auth-specific rate limiting
    modulo_auth_rate_limit_enabled: bool = Field(True)
    modulo_auth_max_attempts: int = Field(10, ge=1)
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

    # Space-separated list of additional CSP source expressions for connect-src.
    # Used for custom Grafana Faro collectors, self-hosted Sentry instances, etc.
    # Entries are validated to not contain semicolons (which would break CSP).
    modulo_monitor_domains: str = Field("")

    modulo_dev_mode: bool = Field(
        default=False,
        description="Enable preview/in-development features (MODULO_DEV_MODE)",
    )

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

    # SMTP — self-hosted email sending. When smtp_host is empty, email dispatch
    # is disabled (logs a warning and returns silently).
    smtp_host: str = Field("")
    smtp_port: int = Field(587)
    smtp_username: str = Field("")
    smtp_password: str = Field(default="", repr=False)
    email_from: str = Field("")

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

    @field_validator("modulo_monitor_domains")
    @classmethod
    def _validate_monitor_domains(cls, v: str) -> str:
        if ";" in v:
            raise ValueError("MODULO_MONITOR_DOMAINS must not contain semicolons (would break CSP)")
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
            url = "postgresql+asyncpg://" + url[len("postgres://") :]
        if url.startswith("mysql+asyncmy://"):
            url = "mysql+aiomysql://" + url[len("mysql+asyncmy://") :]
            _log.warning("settings.legacy_asyncmy_url_replaced")
        # asyncpg doesn't understand sslmode in URLs — strip it so it doesn't
        # cause a URL parsing error. The actual SSL mode is set via
        # connect_args["ssl"] in get_or_create_engine() (dependencies.py); that
        # module MUST always set ssl=False for Postgres to match this.
        url = url.replace("?sslmode=disable", "").replace("&sslmode=disable", "")
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
                self.database_url = "mysql+aiomysql://modulo:modulo@localhost:5435/modulo"
                _log.info("settings.database_url_auto_set", extra={"database_url": self.database_url})
        return self

    @model_validator(mode="after")
    def _saq_test_pause_guard(self) -> "Settings":
        """SAQ_TEST_PAUSE is TEST-ONLY: refuse it outside test/staging.

        SAQ is always the dispatch path post-cutover, so the guard is simply
        ``debug=false`` (no SAQ_ENABLED to combine with).
        """
        if self.saq_test_pause and not self.debug:
            raise ValueError(
                "SAQ_TEST_PAUSE is a TEST-ONLY flag and cannot be used outside test/staging "
                "(set DEBUG=true in test/staging environments)."
            )
        return self


@lru_cache
def get_settings(_fresh: bool = False) -> Settings:
    return Settings()
