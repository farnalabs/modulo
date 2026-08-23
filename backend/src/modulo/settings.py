import logging
from decimal import Decimal
from functools import lru_cache

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings

_log = logging.getLogger(__name__)

_MIN_KEY_LEN = 32
_POSTGRES_ASYNC_PREFIX = "postgresql+asyncpg://"
# Minimum length for each operator secret (MODULO_BREAK_GLASS_SECRET /
# _STANDBY_SECRET). Validated unconditionally when set (break-glass plan §3).
_MIN_BREAK_GLASS_SECRET_LEN = 24
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
    modulo_license_key: str = Field("")
    # Ed25519 public key (hex) for license signature verification.
    # Defaults to dev/test key — set MODULO_LICENSE_PUBLIC_KEY in production.
    modulo_license_public_key: str = Field("")
    # Ed25519 private key (hex) used to SIGN team license keys issued via
    # the admin license-issue endpoint and the Stripe purchase fulfilment
    # webhook (FAR-179). Empty (default) disables issuance — signing fails
    # closed until the operator configures it.
    modulo_license_private_key: str = Field(default="", repr=False)

    # Stripe billing — purchase fulfilment webhook (FAR-178). Empty defaults
    # keep the app bootable without Stripe configured; when both keys are set,
    # ``stripe_enabled`` is True and the /api/v1/webhooks/stripe webhook is
    # active. Stored repr=False so they never leak into logs/settings dumps.
    # Field names uppercase to STRIPE_SECRET_KEY / STRIPE_WEBHOOK_SECRET.
    stripe_secret_key: str = Field(default="", repr=False)
    stripe_webhook_secret: str = Field(default="", repr=False)

    # Hosted community library — client sync (FAR-363). Empty defaults keep the
    # app bootable without a library configured; when ``modulo_library_endpoint``
    # is non-empty the SAQ ``library_sync`` cron polls + verifies the signed
    # manifest and caches the catalog. The root public key is the Ed25519 PEM
    # that verifies manifests — stored repr=False so it never leaks into logs.
    modulo_library_endpoint: str = Field("")
    modulo_library_root_public_key: str = Field(default="", repr=False)
    modulo_library_sync_interval_seconds: int = Field(300)
    modulo_library_sync_timeout_seconds: int = Field(15)

    # SSO / OIDC — JSON array of {provider_id, client_id, client_secret, discovery_url}
    modulo_oidc_providers: str = Field("[]")

    # SSO / SAML 2.0 (team tier, requires license key)
    modulo_saml_enabled: bool = Field(False)
    modulo_saml_idp_metadata_url: str = Field("")
    modulo_saml_idp_metadata_xml: str = Field("")
    modulo_saml_entity_id: str = Field("modulo")
    modulo_saml_sp_private_key: str = Field("")
    modulo_saml_sp_x509_cert: str = Field("")

    # SSO defaults
    modulo_sso_default_role: str = Field("runner")

    # ------------------------------------------------------------------
    # Break-glass admin recovery (deliverable B) — operator CLI + watchdog
    # ------------------------------------------------------------------
    # Independently settable; defaults from primary OR standby secret presence.
    modulo_break_glass_enabled: bool | None = Field(default=None)
    # Operator secrets (primary + rotation standby). Must never equal each
    # other and each must meet the minimum length when set.
    modulo_break_glass_secret: str = Field(default="", repr=False)
    modulo_break_glass_standby_secret: str = Field(default="", repr=False)
    # TTL bounds — enforced at Settings construction (blocking regardless of
    # ENABLED) AND re-enforced at CLI invocation time.
    modulo_break_glass_ttl_minutes: int = Field(default=1440, ge=1)
    modulo_break_glass_max_ttl_minutes: int = Field(default=4320, ge=1, le=4320)
    # Dedicated modulo_breakglass LOGIN engine URL (the CLI connects as this
    # role — never the app database_url).
    modulo_break_glass_database_url: str = Field(default="")
    # Dedicated modulo_system LOGIN engine URL for cross-org system cron jobs
    # (analytics_facts_maintenance, journey_reconcile, retention_cleanup,
    # dispatcher_reconcile). When set, system crons use this URL to connect as
    # the modulo_system role (LOGIN, BYPASSRLS) instead of modulo_app.
    modulo_system_database_url: str = Field(default="")
    # warn|fail — the URL/secret-presence boot checks are warn-in-warn-mode;
    # the allow-list/role-posture assertions (bootstrap_role.py) are FATAL in
    # both modes and are enforced separately at their call site.
    modulo_break_glass_boot_failure_mode: str = Field(default="warn")

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

    # SAQ job heartbeat knob (per-job). The DB heartbeat cadence is
    # RUN_HEARTBEAT_SECONDS below.
    saq_job_heartbeat: int = Field(default=300, alias="SAQ_JOB_HEARTBEAT", ge=1, le=3600)
    # DB heartbeat cadence for execute_run — keep well below the 300s SAQ sweep
    # threshold (cadence equal to the sweep threshold leaves zero margin).
    run_heartbeat_seconds: int = Field(default=30, alias="RUN_HEARTBEAT_SECONDS", ge=1, le=120)
    # FAR-296 Phase 3b: per-run runner-role API-key TTL floor for script-mode
    # sandboxes. The effective TTL is max(this, node_timeout + 5 min), capped by
    # the org-level ``run_api_key_max_ttl_seconds`` settings_json knob (default
    # 1 hour). A leaked per-run key expires quickly by construction.
    run_api_key_default_ttl_seconds: int = Field(default=900, alias="RUN_API_KEY_DEFAULT_TTL_SECONDS", ge=300, le=86400)
    # Cutover hold gate: healthz/ready 503-gates when THIS machine's SAQ workers
    # are stale (default true during the hold). Set false via deploy-time flag
    # after the hold to relax the gate to degraded (alerting continues).
    saq_hard_gate: bool = Field(default=True, alias="SAQ_HARD_GATE")
    # A1 elevation flag (agent-failure UX, phase 1): when a captured sandbox
    # node output self-reports agent_status=failed OR outcome=failed, the run
    # terminalizes as ``failed`` with error_code ``agent.failed`` instead of
    # landing ``complete``. Rollout flag — agent-failure-ux-proposal §15.4.
    modulo_agent_failure_elevation_enabled: bool = Field(True, alias="MODULO_AGENT_FAILURE_ELEVATION_ENABLED")
    # FAR-228 kill-switch for the sandbox idempotency gate (opt-in per node via
    # PipelineGraphNode.delivery_sentinel). Consumed at BOTH guards: guard A
    # (node_runner early skip) and guard B (executor retry suppression). When
    # disabled the gate never fires and transient node failures retry exactly as
    # before. This flag MUST stay consumed at both call sites.
    modulo_idempotency_gate_enabled: bool = Field(True, alias="MODULO_IDEMPOTENCY_GATE_ENABLED")
    # Web UI auth — FAIL-CLOSED (system worker refuses to boot without both).
    saq_auth_password: str | None = Field(default=None, alias="SAQ_AUTH_PASSWORD", repr=False)
    saq_auth_username: str | None = Field(default=None, alias="SAQ_AUTH_USERNAME")
    # SAQ retry knobs — retries=N is N TOTAL attempts (N-1 retries).
    saq_run_retries: int = Field(default=5, alias="SAQ_RUN_RETRIES", ge=1, le=20)
    # Deterministic fixed delay (retry_backoff=False).
    saq_retry_delay: int = Field(default=60, alias="SAQ_RETRY_DELAY", ge=1, le=3600)
    # Per-run execution ceiling for SAQ execute_run (dispatch.py): the job must
    # reach a terminal state within this budget or the worker fails it.
    saq_run_timeout: int = Field(default=7200, alias="SAQ_RUN_TIMEOUT", ge=300, le=86400)
    # Per-claim cap on SAQ claim attempts for dispatcher='saq' runs (F3a).
    # Single source of truth (retro item 9): execute and resume claims in
    # pipeline_execution resolve this value via _resolve_claim_cap; cron_helpers
    # reads it directly.
    saq_run_claim_cap: int = Field(default=20, alias="SAQ_RUN_CLAIM_CAP", ge=1, le=100)
    # TEST-ONLY pause flag — hard default off; refused outside test/staging
    # (debug=false).
    saq_test_pause: bool = Field(default=False, alias="SAQ_TEST_PAUSE")
    # Legacy sweep windows (never_dispatched / worker_lost / re-enqueue) match
    # today's beat-sweep values (5 min / 10 min; re-enqueue is SAQ-only, 600 is
    # 600), decoupled from
    # RUN_CLAIM_STALE_SECONDS (SAQ only).
    saq_reenqueue_window: int = Field(default=600, alias="SAQ_REENQUEUE_WINDOW", ge=1, le=3600)
    saq_never_dispatched_window: int = Field(default=300, alias="SAQ_NEVER_DISPATCHED_WINDOW", ge=1, le=3600)
    saq_worker_lost_window: int = Field(default=600, alias="SAQ_WORKER_LOST_WINDOW", ge=1, le=3600)
    # Zombie-run protection (2026-08-05). A run claimed by SAQ must dispatch at
    # least one node within this setup window or the execute_run zombie watchdog
    # fails it. Covers the pre-node hang window: checkpointer setup, graph
    # compile, connector/model-backend hub init, and the first astream_events
    # super-step. MUST exceed any legitimate pre-first-node setup but stay well
    # below the run_claim_stale_seconds claim fence so the watchdog wins before
    # a stale-heartbeat re-claim can double-execute the hung run.
    saq_setup_grace_seconds: int = Field(default=600, alias="SAQ_SETUP_GRACE_SECONDS", ge=60, le=3600)
    # FAR-369: absolute node-deadline watchdog fallback. When a node's
    # ``timeout_seconds`` is not present in the graph (or the node id is
    # unknown), the watchdog holds the node to this default. Intentionally a
    # generous floor: every real sandbox_agent node ships its own
    # ``timeout_seconds`` (e.g. 1200s), and the watchdog keys off that value —
    # this only guards the degenerate case. Never smaller than the smallest
    # legitimate node timeout.
    saq_node_default_timeout_seconds: int = Field(
        default=1200, alias="SAQ_NODE_DEFAULT_TIMEOUT_SECONDS", ge=30, le=7200
    )
    # dispatcher_reconcile secondary net: a SAQ run still 'running' with a FRESH
    # heartbeat but ZERO LangGraph checkpoints for its thread after this many
    # minutes is a claimed-but-never-executed zombie (the execute_run watchdog
    # normally fails it at SAQ_SETUP_GRACE_SECONDS; this branch catches the case
    # where the worker process itself is wedged and cannot run the watchdog).
    # MUST exceed the pipeline's max node timeout so a legitimate long first
    # node (which writes its first checkpoint only after completing) is never
    # failed. Default 35 min > the 1800s node timeout used by agent pipelines
    # (5 min margin for graph compile + reconcile tick cadence). Reduced from
    # 45 min by FAR-199: a wedged worker fleet left claimed-but-nodeless runs
    # 'running' for the whole window (observed executor_stalled /
    # never_dispatched runs sitting 45-70 min before terminalization); 35 min
    # bounds that accumulation while staying above the max node timeout so a
    # slow-but-healthy first node is never false-failed. Tunable per deploy.
    saq_claimed_nodeless_minutes: int = Field(default=35, alias="SAQ_CLAIMED_NODELESS_MINUTES", ge=5, le=1440)
    # SAQ worker DB pool size (per worker; Postgres budget — F4).
    # Default 30. The pool MUST stay >= SAQ_WORKER_CONCURRENCY + reserve (5):
    # every concurrent run holds a connection in pre-node setup (claim_run_async
    # / load_and_setup / executor.execute), and the watchdog's terminalize write
    # (fail_run_terminal in zombie_watchdog) also needs a connection when a run
    # wedges during that window. With concurrency=20 and only the firefight-era
    # pool of 10, the worker can exhaust its pool in setup and the watchdog
    # cannot get a slot to terminalize — the run rides to the 35-min
    # dispatcher_reconcile backstop as a nodeless zombie. saq_worker._get_async_engine
    # enforces pool >= concurrency + 5 at runtime (raising the effective pool
    # above the configured value with a warning when needed), so no config can
    # wedge the worker on DB connection starvation — exactly like the Redis
    # pool guard (_effective_redis_pool_size). The le=200 cap bounds the
    # configured value; the runtime effective pool may exceed it (it is a
    # runtime override), matching the Redis guard's behaviour.
    saq_worker_db_pool_size: int = Field(default=65, alias="SAQ_WORKER_DB_POOL_SIZE", ge=1, le=200)
    # SAQ Redis client pool size (Upstash connection budget — F2).
    # Default 20 satisfies the invariant for the default concurrency of 5
    # (20 >= 5 + 5 reserve). NOTE the real invariant: SAQ's dequeue() uses a
    # blocking blmove (_DEQUEUE_TIMEOUT) that is NOT gated by
    # max_concurrent_ops — every concurrent _process task holds ONE pool
    # connection while blocked. The Redis pool MUST therefore be strictly
    # larger than the worker concurrency, with reserve for the Upkeep ops
    # (schedule/sweep/abort/jobs/worker_info). saq_worker._build_queue()
    # enforces pool >= concurrency + 5 at runtime (raising the effective pool
    # above the configured value with a warning when needed), so no config can
    # wedge the workers. 2026-08-10 incident: concurrency 20 with the pool
    # secret pinned to 5 silently wedged production — the upkeep task died
    # with ConnectionError: Too many connections and worker_info heartbeats
    # stopped. Prod now pins SAQ_WORKER_CONCURRENCY=20 and SAQ_REDIS_POOL_SIZE=50
    # via secrets; the code guard makes any smaller pool self-correcting.
    saq_redis_pool_size: int = Field(default=20, alias="SAQ_REDIS_POOL_SIZE", ge=1, le=50)
    # SAQ worker concurrency (how many jobs run at once per worker).
    # Default 5; prod/staging pin this to 20 via fly.toml — the accepted
    # design target (20 per worker x up to 5 machines = up to 100 concurrent
    # runs), verified-safe against the prod Postgres 300-connection cap (only
    # ~40 in use; SAQ is asyncio single-engine so concurrency does not multiply
    # the DB pool). Must stay strictly below the effective Redis pool — see
    # saq_redis_pool_size (blocking dequeue holds one pool connection per
    # concurrent task; a pool <= concurrency wedges the worker).
    saq_worker_concurrency: int = Field(default=5, alias="SAQ_WORKER_CONCURRENCY", ge=1, le=50)
    # Per-run agent runtime cost: E2B sandbox hourly rate used to estimate
    # sandbox_agent node cost from wall-clock time (E2B bills per-second
    # sandbox uptime). Default reflects the dashboard-confirmed opencode
    # template = 2 vCPU / 2 GiB at E2B per-second rates (~$0.133/hr).
    e2b_sandbox_usd_per_hour: float = Field(default=0.13, alias="E2B_SANDBOX_USD_PER_HOUR", ge=0)

    # ------------------------------------------------------------------
    # Cost-tracking knobs (multi-component per-run cost tracking — PR A1)
    # ------------------------------------------------------------------
    # These are the operator-tunable anti-abuse bounds for self-reported
    # model cost. All are Decimal-typed so the runtime never mixes float and
    # Decimal min()/comparison operations. A violating env value fails at
    # Settings LOAD (fail-fast), never silently accepted.
    #
    # The floor: any self-reported model_cost_usd below this is NOT a report
    # (closes the spend-evasion hole where a 1e-9 report suppressed the
    # estimate). ge-bounded so a sub-floor knob cannot silently disable the
    # floor.
    max_reportable_usd_min: Decimal = Field(
        default=Decimal("0.000001"),
        alias="MODULO_MAX_REPORTABLE_USD_MIN",
        ge=Decimal("0.000001"),
    )
    # The per-node clamp: an absurd single-node report is clamped here. The
    # WRITE-PATH effective value is min(knob, Decimal("99999999.999999")) —
    # the Numeric(14,6) column cap — so a 1e9 env value cannot silently
    # disable the load-bearing per-node clamp. ge-bounded (a sub-floor knob
    # would disable the floor).
    max_self_reported_usd: Decimal = Field(
        default=Decimal("10000.0"),
        alias="MODULO_MAX_SELF_REPORTED_USD",
        ge=Decimal("0.000001"),
    )
    # The band ceiling — the TOP OF THE SANITY BAND, the trust boundary for
    # self-reported model cost at the backend extraction boundary. Any
    # producer is clamped here (band < per-node cap by default, so the band
    # dominates). ge-bounded like the other knobs.
    max_reportable_band_usd: Decimal = Field(
        default=Decimal("50.0"),
        alias="MODULO_MAX_REPORTABLE_BAND_USD",
        ge=Decimal("0.000001"),
    )
    # Dynamic rate_usd bound for cost_components writes: a rate above this is
    # rejected 422. The WRITE-PATH effective value is
    # min(knob, Decimal("999999999999.999999")) — the Numeric(18,6) column
    # cap. ge-bounded at 0.
    max_rate_usd: Decimal = Field(
        default=Decimal("100000.0"),
        alias="MODULO_MAX_RATE_USD",
        ge=Decimal(0),
    )

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

    # ------------------------------------------------------------------
    # In-process worker-liveness watchdog (postmortem 2026-08-09, FAR-121)
    # ------------------------------------------------------------------
    # A plain asyncio task in the WEB-process FastAPI lifespan (NOT an SAQ
    # cron — if the workers are down the cron path is down) reads SAQ worker
    # liveness directly from Redis every tick and fans an alert out to every
    # configured channel (generic webhook / Teams webhook / email) when every
    # worker is dead. Default-off: no alert is sent until the operator
    # configures at least one of ALERT_WEBHOOK_URL, ALERT_TEAMS_WEBHOOK_URL,
    # or ALERT_EMAIL_TO (+ SMTP_*).
    watchdog_enabled: bool = Field(default=True, alias="WATCHDOG_ENABLED")
    watchdog_tick_seconds: int = Field(default=30, alias="WATCHDOG_TICK_SECONDS", ge=5, le=600)
    watchdog_worker_stale_seconds: int = Field(default=180, alias="WATCHDOG_WORKER_STALE_SECONDS", ge=60, le=3600)
    watchdog_alert_state_ttl_seconds: int = Field(
        default=7 * 24 * 3600, alias="WATCHDOG_ALERT_STATE_TTL_SECONDS", ge=3600, le=60 * 24 * 3600
    )  # edge-triggered state: outlives any incident so no repeat alerts
    # Slack-compatible webhook URL for watchdog alerts. None (default) = the
    # watchdog ticks and logs but never POSTs — the operator must configure
    # this in production for alerts to fire.
    alert_webhook_url: str | None = Field(default=None, alias="ALERT_WEBHOOK_URL", repr=False)
    # Microsoft Teams incoming webhook URL (MessageCard payload) for watchdog
    # alerts. None (default) = the Teams channel is disabled.
    alert_teams_webhook_url: str | None = Field(default=None, alias="ALERT_TEAMS_WEBHOOK_URL", repr=False)
    # Comma-separated email recipients for watchdog alerts. Parsed on send
    # (whitespace-trimmed, empties dropped). None or empty = the email channel
    # is disabled — email also requires smtp_host to be set.
    alert_email_to: str | None = Field(default=None, alias="ALERT_EMAIL_TO", repr=False)

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

    # Product analytics — opt-in aggregate usage metrics (design doc section 11).
    # Endpoint URL for the vendor service. Empty = disabled.
    product_analytics_endpoint_url: str = Field(default="", alias="MODULO_PRODUCT_ANALYTICS_ENDPOINT_URL")
    # Instance secret for HMAC signing. Empty = disabled.
    product_analytics_instance_secret: str = Field(
        default="", alias="MODULO_PRODUCT_ANALYTICS_INSTANCE_SECRET", repr=False
    )

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
    # /api/v1/webhooks is exempt because it hosts the Stripe purchase webhook,
    # which authenticates via the Stripe-Signature header (signed payload) —
    # cookie-independent, so a CSRF cookie check would 403 it for the same
    # reason the /api/v1/triggers HMAC webhooks are exempt.
    modulo_csrf_exempt_paths: str = Field("/api/v1/health,/api/v1/triggers,/api/v1/auth,/api/v1/webhooks")

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
    # SMTP connection/send timeout in seconds (per attempt). Org-level email
    # settings can override this per-organisation via the admin email-settings API.
    smtp_timeout: int = Field(30, ge=1, le=120)

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
            url = _POSTGRES_ASYNC_PREFIX + url[len("postgres://") :]
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
            if self.database_url.startswith(_POSTGRES_ASYNC_PREFIX):
                self.database_url = "sqlite+aiosqlite:///./modulo.db"
                _log.info("settings.database_url_auto_set", extra={"database_url": self.database_url})
        elif self.modulo_db.lower() in ("mariadb", "mysql"):
            _log.warning("settings.mariadb_mode")
            if self.database_url.startswith(_POSTGRES_ASYNC_PREFIX):
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

    @model_validator(mode="after")
    def _warn_saq_setup_grace_ge_claim_stale(self) -> "Settings":
        """WARN-only: the setup grace window must stay below the claim fence.

        ``saq_setup_grace_seconds`` is the execute_run zombie-watchdog window (a
        run that has not dispatched a node within it is failed), while
        ``run_claim_stale_seconds`` is the stale-heartbeat re-claim fence. If the
        grace window is >= the stale fence, a legitimately slow run can be
        re-claimed (and double-executed) before its own watchdog fires. This is a
        WARN, never a raise — the currently deployed config intentionally has
        grace=600 > stale=450.
        """
        if self.saq_setup_grace_seconds >= self.run_claim_stale_seconds:
            _log.warning(
                "settings.saq_setup_grace_ge_claim_stale",
                extra={
                    "saq_setup_grace_seconds": self.saq_setup_grace_seconds,
                    "run_claim_stale_seconds": self.run_claim_stale_seconds,
                },
            )
        return self

    @model_validator(mode="after")
    def _finalize_break_glass_config(self) -> "Settings":
        """Resolve ENABLED default + validate the break-glass config matrix.

        Blocking (unconditional) checks, per the break-glass plan §3:
        * ENABLED defaults from (primary OR standby) secret presence when the
          env var was not explicitly provided.
        * SECRET and STANDBY must differ when both are set (rotation path).
        * each non-empty secret meets the minimum length.
        * TTL_MINUTES < 1, MAX_TTL_MINUTES > 4320, or TTL > MAX all fail here
          regardless of ENABLED.
        * BOOT_FAILURE_MODE must be 'warn' or 'fail'.

        The URL/secret-PRESENCE checks (ENABLED=true + empty secrets/URL, warn
        if either empty otherwise) are NOT here — they are boot-time findings
        that honour warn|fail mode via ``break_glass_boot_findings``.
        """
        if self.modulo_break_glass_enabled is None:
            self.modulo_break_glass_enabled = bool(
                self.modulo_break_glass_secret or self.modulo_break_glass_standby_secret
            )

        secret = self.modulo_break_glass_secret
        standby = self.modulo_break_glass_standby_secret
        if secret and standby and secret == standby:
            raise ValueError(
                "MODULO_BREAK_GLASS_SECRET and MODULO_BREAK_GLASS_STANDBY_SECRET must differ "
                "(identical secrets break the rotation path)"
            )
        for name, value in (
            ("MODULO_BREAK_GLASS_SECRET", secret),
            ("MODULO_BREAK_GLASS_STANDBY_SECRET", standby),
        ):
            if value and len(value) < _MIN_BREAK_GLASS_SECRET_LEN:
                raise ValueError(f"{name} must be at least {_MIN_BREAK_GLASS_SECRET_LEN} characters; got {len(value)}")

        if self.modulo_break_glass_ttl_minutes > self.modulo_break_glass_max_ttl_minutes:
            raise ValueError(
                "MODULO_BREAK_GLASS_TTL_MINUTES must be <= MODULO_BREAK_GLASS_MAX_TTL_MINUTES "
                f"({self.modulo_break_glass_ttl_minutes} > {self.modulo_break_glass_max_ttl_minutes})"
            )

        if self.modulo_break_glass_boot_failure_mode not in ("warn", "fail"):
            raise ValueError(
                "MODULO_BREAK_GLASS_BOOT_FAILURE_MODE must be 'warn' or 'fail'; "
                f"got {self.modulo_break_glass_boot_failure_mode!r}"
            )
        return self

    @model_validator(mode="after")
    def _validate_cost_knobs(self) -> "Settings":
        """Cost-knob invariants — enforced at Settings LOAD, never log-only.

        A violating combination is UNREPRESENTABLE: every process that
        constructs Settings (including the SAQ system worker) fails fast
        identically, and the boot self-test surfaces the operator-facing
        recovery message.

        All comparisons are DECIMAL-TYPED: knobs are coerced with
        Decimal(str(value)) so a float/str env override cannot raise
        TypeError or compare lexicographically.

        Guards:
        1. Ordering invariant — max_reportable_usd_min >= max_self_reported_usd
           would silently disable self-reporting (no report can clear the
           floor), so it raises.
        2. Floor-vs-band guard (BOOT-FATAL) — max_reportable_usd_min >=
           max_reportable_band_usd omits every plausible report (reports are
           bounded by the band ceiling), so it raises.
        3. Knob-below-band guard — max_reportable_band_usd > max_self_reported_usd
           makes the out_of_band_high marker unreachable (no report can exceed
           the band), so it raises.
        """
        floor = Decimal(str(self.max_reportable_usd_min))
        clamp = Decimal(str(self.max_self_reported_usd))
        band = Decimal(str(self.max_reportable_band_usd))
        if floor >= clamp:
            raise ValueError(
                "Cost-knob ordering violation: MODULO_MAX_REPORTABLE_USD_MIN ("
                f"{floor}) >= MODULO_MAX_SELF_REPORTED_USD ({clamp}) — a floor at "
                "or above the per-node clamp silently disables self-reporting. "
                "Set MODULO_MAX_REPORTABLE_USD_MIN to a value strictly below "
                f"MODULO_MAX_SELF_REPORTED_USD (valid range: 0.000001 .. {clamp})."
            )
        if floor >= band:
            raise ValueError(
                "Cost-knob floor-vs-band violation (BOOT-FATAL): "
                f"MODULO_MAX_REPORTABLE_USD_MIN ({floor}) >= "
                f"MODULO_MAX_REPORTABLE_BAND_USD ({band}) — a floor at or above the "
                "band ceiling omits EVERY plausible self-report. Set "
                f"MODULO_MAX_REPORTABLE_USD_MIN strictly below {band} (valid "
                f"range: 0.000001 .. {band})."
            )
        if band > clamp:
            raise ValueError(
                "Cost-knob knob-below-band violation (BOOT-FATAL): "
                f"MODULO_MAX_REPORTABLE_BAND_USD ({band}) > "
                f"MODULO_MAX_SELF_REPORTED_USD ({clamp}) — the out_of_band_high "
                "marker can never fire because no report can exceed the band. "
                f"Set MODULO_MAX_REPORTABLE_BAND_USD <= {clamp} (default 50.0)."
            )
        return self

    @property
    def effective_max_self_reported_usd(self) -> Decimal:
        """The write-path per-node clamp: min-capped at the Numeric(14,6) column cap."""
        return min(Decimal(str(self.max_self_reported_usd)), Decimal("99999999.999999"))

    @property
    def effective_max_rate_usd(self) -> Decimal:
        """The write-path rate_usd bound: min-capped at the Numeric(18,6) column cap."""
        return min(Decimal(str(self.max_rate_usd)), Decimal("999999999999.999999"))

    @property
    def stripe_enabled(self) -> bool:
        """True when both Stripe keys are configured (purchase fulfilment active)."""
        return bool(self.stripe_secret_key and self.stripe_webhook_secret)


@lru_cache
def get_settings(_fresh: bool = False) -> Settings:
    return Settings()


def break_glass_boot_findings(settings: Settings) -> list[tuple[bool, str]]:
    """Return ``(blocking, message)`` boot findings (empty list = clean).

    These are the URL/secret-PRESENCE checks that honour
    ``MODULO_BREAK_GLASS_BOOT_FAILURE_MODE``: blocking findings raise in
    fail-mode and warn in warn-mode; non-blocking findings always warn and
    never fail boot. The TTL bounds, SECRET == STANDBY, and minimum-length
    checks raise at Settings construction time (unconditional) so they never
    appear here.

    Blocking (ENABLED=true gated):
    * ENABLED=true with both secrets empty.
    * ENABLED=true with empty MODULO_BREAK_GLASS_DATABASE_URL.

    Non-blocking:
    * either secret empty (rotation path degraded).
    * ENABLED=false with empty URL.
    """
    findings: list[tuple[bool, str]] = []
    enabled = bool(settings.modulo_break_glass_enabled)
    has_primary = bool(settings.modulo_break_glass_secret)
    has_standby = bool(settings.modulo_break_glass_standby_secret)
    has_url = bool(settings.modulo_break_glass_database_url)

    if enabled and not (has_primary or has_standby):
        findings.append(
            (
                True,
                "MODULO_BREAK_GLASS_ENABLED=true but both MODULO_BREAK_GLASS_SECRET and "
                "MODULO_BREAK_GLASS_STANDBY_SECRET are empty",
            )
        )
    elif not (has_primary and has_standby):
        findings.append(
            (
                False,
                "one of MODULO_BREAK_GLASS_SECRET / MODULO_BREAK_GLASS_STANDBY_SECRET is empty — "
                "the operator rotation path is degraded",
            )
        )

    if enabled and not has_url:
        findings.append((True, "MODULO_BREAK_GLASS_ENABLED=true but MODULO_BREAK_GLASS_DATABASE_URL is empty"))
    elif not has_url:
        findings.append(
            (
                False,
                "MODULO_BREAK_GLASS_DATABASE_URL is empty — the break-glass CLI "
                "deactivate/force/status commands are inoperable while disabled",
            )
        )
    return findings


def validate_break_glass_boot(settings: Settings) -> None:
    """Boot-time break-glass config assertion honouring warn|fail mode.

    In ``fail`` mode any BLOCKING finding raises ``RuntimeError`` (boot fails);
    non-blocking findings always log at WARNING and boot continues. In ``warn``
    mode every finding is logged at WARNING. The allow-list / role-posture
    assertions from ``bootstrap_role.py`` are FATAL in both modes and are
    enforced separately at their call site.
    """
    findings = break_glass_boot_findings(settings)
    if not findings:
        return
    if settings.modulo_break_glass_boot_failure_mode == "fail":
        blocking = [message for is_blocking, message in findings if is_blocking]
        if blocking:
            raise RuntimeError("Break-glass boot config assertion FAILED:\n  " + "\n".join(blocking))
    for _is_blocking, message in findings:
        _log.warning("break_glass.boot_config %s", message)
