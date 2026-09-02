# Configuration Reference

Complete reference for all environment variables supported by Modulo. Variables are grouped by function.

---

## Required

| Variable | Description | Example |
|----------|-------------|---------|
| `DATABASE_URL` | Async PostgreSQL connection string | `postgresql+asyncpg://modulo:pass@localhost:5434/modulo` |
| `SECRET_KEY` | JWT signing key, minimum 32 bytes | `openssl rand -base64 32` |
| `FERNET_KEY` | Fernet encryption key, 44-char base64 | `python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |

The application refuses to start if any required variable is absent or invalid.

---

## Database

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `DATABASE_URL` | **Yes** | – | `postgresql+asyncpg://user:pass@host:port/db` |
| `MODULO_DB` | No | `postgres` | Database backend: `postgres`, `sqlite`, `mariadb`, or `mysql` |

`MODULO_DB=sqlite` switches to SQLite for local development (no RLS, no advisory locks, no flood protection).
`MODULO_DB=mariadb` or `mysql` uses the aiomysql driver (MariaDB is deprecated since 2026-07-11).
See [`docs/system-requirements.md`](./system-requirements.md) for backend limitations.

---

## Authentication & Secrets

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `SECRET_KEY` | **Yes** | – | JWT signing key, minimum 32 bytes (256 bits) |
| `FERNET_KEY` | **Yes** | – | Fernet encryption key, exactly 44 base64-encoded bytes |
| `FERNET_KEY_OLD` | No | – | Previous Fernet key for no-downtime rotation; decrypt falls back to this when `FERNET_KEY` is rotated |
| `MODULO_USERS` | For seeding | – | Comma-separated `user:pass` pairs for initial user seed |
| `MODULO_ADMIN_PASSWORD` | No | – | Admin password for single-admin alpha auth (at least one of `MODULO_ADMIN_PASSWORD` or `MODULO_USERS` must be set) |
| `MODULO_ADMIN_SECRET` | No | – | **CLI-only**. Shared secret for `modulo-migrate` CLI auth bypass (not part of the Settings class; read directly by the CLI tool) |
| `MODULO_ADMIN_TOKEN` | No | – | **CLI-only**. Admin JWT for `modulo-migrate` CLI (alternative to env; not part of the Settings class) |
| `MODULO_SECRETS_BACKEND` | No | `fernet` | Secrets backend: `fernet`, `vault`, or `aws` |

See [`docs/deployment-security.md`](./deployment-security.md) for key rotation procedures and [`docs/security/secret-management.md`](./security/secret-management.md) for backend-specific configuration.

---

## License Key

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `MODULO_LICENSE_KEY` | No | – | Base64-encoded signed JSON payload enabling Team-tier features. Verified at startup using the embedded Ed25519 public key. |
| `MODULO_LICENSE_PUBLIC_KEY` | No | – | Ed25519 public key (hex) for license signature verification. Defaults to dev/test key; set in production. |
| `MODULO_LICENSE_PRIVATE_KEY` | No | – | Ed25519 private key (hex) used to SIGN team license keys issued via the admin license-issue endpoint and Stripe purchase fulfilment. Empty disables issuance (signing fails closed). |

---

## Stripe (Purchase Fulfilment)

The `POST /api/v1/webhooks/stripe` webhook verifies the `Stripe-Signature`
header (HMAC-SHA256 over `t=<timestamp>.<body>` with the webhook secret,
±300s replay window), then idempotently generates an Ed25519-signed
team license and emails it to the customer on their first successful
payment (`invoice.paid`). `checkout.session.completed` is treated as a pure
ack and never fulfils, so a single card-paid purchase (which emits both
events) issues exactly one license.

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `STRIPE_SECRET_KEY` | For Stripe | – | Stripe secret key, used for customer email lookups. When both Stripe keys are empty the webhook is inactive. |
| `STRIPE_WEBHOOK_SECRET` | For Stripe | – | Stripe webhook signing secret (`whsec_...`), used to verify `Stripe-Signature`. When both Stripe keys are empty the webhook is inactive. |

---

## SSO / SAML 2.0

Team-tier feature (requires valid `MODULO_LICENSE_KEY`). Configurable via env vars or the admin SSO providers UI.

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `MODULO_OIDC_PROVIDERS` | No | `[]` | JSON array of `{provider_id, client_id, client_secret, discovery_url}` objects. **Deprecated** — use the admin SSO providers UI. |
| `MODULO_SAML_ENABLED` | No | `false` | Enable SAML 2.0 authentication |
| `MODULO_SAML_IDP_METADATA_URL` | No | – | SAML IdP metadata URL |
| `MODULO_SAML_IDP_METADATA_XML` | No | – | SAML IdP metadata XML (alternative to URL) |
| `MODULO_SAML_ENTITY_ID` | No | `modulo` | SAML SP entity ID |
| `MODULO_SAML_SP_PRIVATE_KEY` | No | – | SAML SP private key |
| `MODULO_SAML_SP_X509_CERT` | No | – | SAML SP X.509 certificate |
| `MODULO_SSO_DEFAULT_ROLE` | No | `runner` | Default org role assigned on JIT provisioning |

---

## Server & Networking

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `MODULO_PUBLIC_URL` | For SSO | `http://localhost:8000` | Public-facing URL for OAuth redirects, webhook callbacks, email links |
| `CORS_ORIGINS` | No | `http://localhost:5173` | Comma-separated allowed CORS origins |
| `CORS_MAX_AGE` | No | `600` | Preflight cache max-age in seconds |
| `MODULO_LOG_LEVEL` | No | `INFO` | Logging level: `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `MODULO_WS_TOKEN_TTL_SECONDS` | No | `60` | WebSocket auth token TTL in seconds |
| `DEBUG` | No | `false` | Enable debug mode (test/staging environments) |
| `MODULO_DEV_MODE` | No | `false` | Enable preview / in-development features |
| `INACTIVITY_TIMEOUT_MINUTES` | No | `480` | Session inactivity timeout in minutes (0 to disable) |

---

## Redis & Task Queue

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `REDIS_URL` | No | `redis://localhost:6379/0` | `redis://host:port/db` for the SAQ broker and rate limiting |

Redis is **required** for production: the SAQ workers (runs + system) provide
run dispatch, cron firing, and the scheduler. Without Redis there is no
executor – only in-memory rate limiting and an in-memory event broker.

---

## SAQ (task queue / workers)

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `SAQ_RUNS_QUEUE` | No | `runs` | Runs-queue name (`staging-runs` on staging for isolation) |
| `SAQ_HARD_GATE` | No | `true` | Healthz/ready 503-gates when THIS machine's SAQ workers are stale. Set `false` to relax to degraded (alerting continues). The cutover deploy-hold was retired 2026-08-05 – this readiness gate is the only gate left |
| `SAQ_AUTH_PASSWORD` | Yes (system worker) | – | Fail-closed web UI auth password; refuse to boot without it |
| `SAQ_AUTH_USERNAME` | Yes (system worker) | – | Fail-closed web UI auth user; maps to the `AUTH_USER` env SAQ's web reads |
| `SAQ_RUN_RETRIES` | No | `5` | SAQ retries per run job – `N` is N total attempts (N-1 retries) |
| `SAQ_RETRY_DELAY` | No | `60` | Fixed retry delay in seconds (`retry_backoff=False`) |
| `SAQ_RUN_TIMEOUT` | No | `7200` | Per-run execution ceiling; the job must reach a terminal state within this budget (seconds) |
| `SAQ_RUN_CLAIM_CAP` | No | `20` | Per-claim cap on SAQ claim attempts for `dispatcher='saq'` runs |
| `SAQ_SETUP_GRACE_SECONDS` | No | `600` | Zombie-run protection: a run must dispatch at least one node within this window or the watchdog fails it |
| `SAQ_CLAIMED_NODELESS_MINUTES` | No | `35` | Secondary zombie net: a run still `running` with a fresh heartbeat but zero checkpoints after this many minutes is failed. Reduced from `45` by FAR-199 (bounds wedged-fleet accumulation) — must stay above the 1800s max node timeout so a slow-but-healthy first node is never false-failed |
| `SAQ_JOB_HEARTBEAT` | No | `300` | SAQ job heartbeat knob (per-job `heartbeat`) |
| `SAQ_REENQUEUE_WINDOW` | No | `600` | Re-enqueue staleness window for `dispatcher_reconcile` |
| `SAQ_NEVER_DISPATCHED_WINDOW` | No | `300` | Legacy never-dispatched sweep window (non-SAQ rows only) |
| `SAQ_WORKER_LOST_WINDOW` | No | `600` | Legacy worker-lost sweep window (non-SAQ rows only) |
| `SAQ_WORKER_DB_POOL_SIZE` | No | `10` | SAQ worker Postgres pool size (per worker). Verified 2026-08-06: deployed Postgres `max_connections=300` with ~40 in use at sample time – 10 x 2 workers x up to 5 machines = 100 + web pools + checkpointer fits with headroom. |
| `SAQ_REDIS_POOL_SIZE` | No | `20` | SAQ Redis client pool size (Upstash connection budget). The effective pool is floored at `SAQ_WORKER_CONCURRENCY + 5` (max() in `saq_worker._effective_redis_pool_size` — blocking dequeue holds one connection per concurrent slot, +5 reserve for upkeep ops). Prod pins `50` in `fly.toml`; staging pins `10` in `fly.staging.toml` (2026-09 Redis-usage audit right-size). Operators on a small Redis tier may lower it further. |
| `SAQ_WORKER_CONCURRENCY` | No | `5` | SAQ worker job concurrency, decoupled from Redis pool size. Design target 20/worker x up to 5 machines = up to 100 concurrent runs – verified-safe against the prod Postgres 300-connection cap (SAQ is asyncio single-engine, so concurrency does not multiply the DB pool). Prod pins `20` in `fly.toml` (ADR 017 design target); staging pins `2` in `fly.staging.toml` (2026-09 Redis-usage audit right-size). |
| `RUN_CLAIM_STALE_SECONDS` | No | `450` | Staleness gate for re-claiming a SAQ run whose heartbeat is stale |
| `RUN_HEARTBEAT_SECONDS` | No | `30` | DB heartbeat cadence (keep below the 300s SAQ sweep threshold) |
| `SAQ_TEST_PAUSE` | TEST-ONLY | `false` | Test-only pause flag; refused outside test/staging (`DEBUG=true`) |

`SAQ_HARD_GATE` replaces the removed `SAQ_ENABLED` flag: post-cutover SAQ is the
only dispatch path, so the readiness gate is always active. The deploy-time
`SAQ_HOLD` gate (deploy.yml `hold-check` job) was retired 2026-08-05 – no
deploy hold remains; `SAQ_HARD_GATE` is the only gate.

---

## Observability

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `MODULO_TELEMETRY_ENABLED` | No | `false` | Enable OpenTelemetry instrumentation |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | No | – | OTel gRPC exporter endpoint (e.g. `http://otel-collector:4317`) |
| `MODULO_OTEL_SERVICE_NAME` | No | `modulo` | OTel service name attribute |

Telemetry is opt-in. With default settings, Modulo makes **zero** external network calls. See [`docs/operations/network-egress.md`](./operations/network-egress.md).

---

## Rate Limiting

Rate limits are hardcoded in `RateLimitMiddleware` (see [`backend/src/modulo/api/middleware/rate_limiter.py`](../backend/src/modulo/api/middleware/rate_limiter.py)):

| Path | Limit | Window |
|------|-------|--------|
| POST `/api/v1/runs` | 60 | 60s |
| POST `/api/v1/triggers` | 100 | 60s |
| POST `/api/v1/errors/ingest` | 10 | 60s |
| `/mcp` (all POST/PUT/PATCH) | 200 | 60s |
| Auth endpoints (all POST/PUT/PATCH) | 10 attempts | 60s |

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `MODULO_AUTH_MAX_ATTEMPTS` | No | `10` | Login attempts per sliding window |
| `MODULO_AUTH_RATE_LIMIT_ENABLED` | No | `true` | Enable auth-specific rate limiting |
| `MODULO_AUTH_WINDOW_SECONDS` | No | `60` | Auth rate limit window in seconds |
| `MODULO_RATELIMIT_BYPASS_TOKEN` | No | – | Shared secret to bypass rate limiting (for CI/CD) |

Rate limiting uses Redis sliding window (ZADD + ZREMRANGEBYSCORE). Falls back to in-memory no-op without Redis. Auth rate limiter requires Redis and is disabled without it.

---

## Runtime & Sandbox

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `MODULO_E2B_API_KEY` | For E2B | – | E2B sandbox API key for runtime provider (read directly from env, not via Settings) |
| `MODULO_MAX_LOCAL_CONCURRENCY` | No | `2` | Max concurrent local agents (LocalRuntimeProvider) |
| `E2B_SANDBOX_USD_PER_HOUR` | No | `0.13` | Hourly USD rate for an E2B sandbox, used to estimate per-run agent runtime cost from wall-clock time; default reflects the opencode template (2 vCPU / 2 GiB) rate; set to your E2B sandbox rate. |

---

## Cost Tracking

Anti-abuse knobs for self-reported model cost (see
`docs/design/multi-component-cost-tracking.md`). A violating value fails at
Settings load (fail-fast) – a bad env value blocks boot with a recovery message.

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `MODULO_MAX_REPORTABLE_USD_MIN` | No | `0.000001` | The floor: a self-reported `model_cost_usd` below this is NOT a report (closes the spend-evasion hole). `ge=0.000001` – a sub-floor knob is rejected. |
| `MODULO_MAX_SELF_REPORTED_USD` | No | `10000.0` | The per-node clamp for an absurd single-node report. The write-path effective value is min-capped at `99999999.999999` (the run column cap), so a `1e9` env value cannot silently disable the clamp. `ge=0.000001`. |
| `MODULO_MAX_REPORTABLE_BAND_USD` | No | `50.0` | The band ceiling – the trust boundary for self-reported model cost at the backend extraction boundary. Any producer is clamped here; a value above the band carries the `model_cost_out_of_band_high` marker. Must be `<= MODULO_MAX_SELF_REPORTED_USD` (else boot-fatal). |
| `MODULO_MAX_RATE_USD` | No | `100000.0` | Dynamic upper bound for a component's `rate_usd` on writes. The write-path effective value is min-capped at `999999999999.999999` (the rate column cap). Lowering it does NOT affect existing components – the knob moves the write-path boundary only; existing rows are still evaluated at finalization at their stored rate. |

The knobs are Decimal-typed; all comparisons are Decimal (a float/Decimal
`min()` mismatch is a bug). The ordering invariant
(`MODULO_MAX_REPORTABLE_USD_MIN < MODULO_MAX_SELF_REPORTED_USD`), the
floor-vs-band guard, and the knob-below-band guard are enforced at Settings
LOAD.

---

## Feature Flags

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `MODULO_PLUGIN_DISCOVERY` | No | `true` | Enable automatic plugin discovery |

---

## Organisation Settings (`settings_json`)

Per-organisation configuration is stored in the `settings_json` column of the
`organisations` table (not environment variables). Configured by an org admin
via the admin API. Unknown/absent keys default to safe values.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `sandbox_concurrency_limit` | `int` (1–100) or `null` | `null` (unlimited) | Max concurrently `running` sandbox-agent runs for the org across all pipelines. Runs beyond the cap stay `pending` with `error_code='org_capacity_limited'` and are retried by the background accelerator. Managed via `GET`/`PUT /api/v1/admin/org/sandbox-concurrency`. |
| `run_concurrency_limit` | `int` (1–100) or `null` | `null` (unlimited) | Max concurrently executing/claimed runs for the org across ALL pipelines (sandbox-agent and otherwise). Runs dispatched while the org is at this cap are deferred back to `pending` with `error_code='org_capacity_limited'` and retried by the background accelerator. Independent of `sandbox_concurrency_limit` – both are org-wide caps, and both produce the same `org_capacity_limited` marker on deferred runs. Managed via `GET`/`PUT /api/v1/admin/org/run-concurrency`. |

---

## Backup & Recovery

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `MODULO_BACKUP_PASSPHRASE` | For encryption | – | AES-256-CBC backup encryption passphrase (min 32 chars) |

See [`docs/operations/backup.md`](./operations/backup.md) for backup configuration.

---

## SMTP (Email)

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `SMTP_HOST` | For email | – | SMTP server hostname. When empty, email dispatch is disabled. |
| `SMTP_PORT` | No | `587` | SMTP server port |
| `SMTP_USERNAME` | No | – | SMTP authentication username |
| `SMTP_PASSWORD` | No | – | SMTP authentication password |
| `EMAIL_FROM` | No | – | From-address for outgoing emails |
| `SMTP_TIMEOUT` | No | `30` | SMTP connection/send timeout in seconds |

---

## Worker Liveness Watchdog

An in-process asyncio task running in the web-process FastAPI lifespan that reads SAQ worker liveness from Redis every tick and fires alerts when all workers are dead. No alert is sent until at least one alert channel is configured.

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `WATCHDOG_ENABLED` | No | `true` | Enable the watchdog tick |
| `WATCHDOG_TICK_SECONDS` | No | `30` | Tick interval in seconds |
| `WATCHDOG_WORKER_STALE_SECONDS` | No | `180` | Worker considered stale after this many seconds without heartbeat |
| `WATCHDOG_ALERT_STATE_TTL_SECONDS` | No | `604800` | Edge-triggered alert state TTL (default 7 days) |
| `ALERT_WEBHOOK_URL` | No | – | Slack-compatible webhook URL for watchdog alerts |
| `ALERT_TEAMS_WEBHOOK_URL` | No | – | Microsoft Teams incoming webhook URL |
| `ALERT_EMAIL_TO` | No | – | Comma-separated email recipients for watchdog alerts |

---

## SSE Event Stream

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `MODULO_SSE_MAX_CONNECTIONS_PER_ORG` | No | `100` | Max concurrent SSE connections per org |
| `MODULO_SSE_MAX_CONNECTIONS_PER_USER` | No | `10` | Max concurrent SSE connections per user |
| `MODULO_SSE_ZOMBIE_TIMEOUT_SECONDS` | No | `2.0` | Zombie connection timeout in seconds |

---

## CSRF Protection

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `MODULO_CSRF_ENABLED` | No | `true` | Enable CSRF protection middleware |
| `MODULO_CSRF_EXEMPT_PATHS` | No | `/api/v1/health,/api/v1/triggers,/api/v1/auth` | Comma-separated paths exempt from CSRF |

---

## SCIM Provisioning

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `MODULO_SCIM_TOKEN` | No | – | SCIM bearer token for identity provider provisioning |
| `MODULO_SCIM_DEFAULT_ORG_ID` | No | – | Default org ID for SCIM provisioning; uses first org if empty |

---

## TLS / Connection Security

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `VAULT_ADDR` | For Vault | – | HashiCorp Vault server address |
| `VAULT_TOKEN` | For Vault | – | Vault authentication token |
| `VAULT_ROLE_ID` | For Vault | – | Vault AppRole role ID |
| `VAULT_SECRET_ID` | For Vault | – | Vault AppRole secret ID |
| `AWS_ACCESS_KEY_ID` | For AWS Secrets Manager | – | AWS access key for the Secrets Manager backend |
| `AWS_SECRET_ACCESS_KEY` | For AWS Secrets Manager | – | AWS secret access key |
| `AWS_REGION` | No | `us-east-1` | AWS region for Secrets Manager |
| `AWS_PROFILE` | No | – | AWS profile name for Secrets Manager |

See [`docs/security/secret-management.md`](./security/secret-management.md) for Vault and AWS Secrets Manager configuration.

Secrets backend selection: `MODULO_SECRETS_BACKEND` (default: `fernet`, options: `fernet`, `vault`, `aws`).

---

## Outbound Egress Guard (SSRF)

Every outbound URL Modulo is given by a user or an organisation is validated
before a request is made: notification webhooks, SSO test connections,
observability and error-forwarder tests, all `base_url`-bearing connectors, and
the OpenAI-compatible model backends. Private, loopback, link-local,
cloud-metadata and CGNAT destinations are refused by default.

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `SSRF_ALLOW_PRIVATE_RANGES` | No | – | Comma-separated CIDRs to permit as outbound targets, e.g. `127.0.0.0/8,::1/128,10.0.0.0/8` |
| `SSRF_DNS_TIMEOUT` | No | `10` | DNS resolution timeout in seconds; a hung resolver fails closed |

Link-local (`169.254.0.0/16`, `fe80::/10`), multicast, IPv6 site-local and the
cloud-metadata ranges are a **non-negotiable floor** — no allowlist entry can
make them reachable.

### Self-hosted targets on localhost

Reaching a service on the host (a local Ollama / vLLM / LM Studio model backend,
or a connector left on its localhost default) requires an explicit opt-in:

```bash
SSRF_ALLOW_PRIVATE_RANGES=127.0.0.0/8,::1/128
```

**Both entries are required.** `localhost` resolves to `127.0.0.1` *and* `::1` on
a dual-stack host, and validation fails closed if any resolved address is
blocked — allowlisting only `127.0.0.0/8` leaves `http://localhost:11434`
unreachable. Alternatively, use a literal `http://127.0.0.1:11434` URL, which
skips DNS resolution entirely and needs only `127.0.0.0/8`.

These connectors ship a localhost default `base_url`, so they need the opt-in
above (or an explicit non-loopback `base_url`) before they will connect:

| Connector | Default `base_url` |
|-----------|--------------------|
| Trivy | `http://localhost:8080` |
| SonarQube | `http://localhost:9000` |
| 1Password Connect | `http://localhost:8080` |
| TeamCity | `http://localhost:8111` |
| Jenkins | `http://localhost:8080` |
| n8n | `http://localhost:5678` |
| Grafana | `http://localhost:3000` |

Without the opt-in, these fail with a `ValueError` naming the blocked address and
the exact variable to set; connector health checks surface the same text as an
unhealthy detail rather than raising.

### Scope

`SSRF_ALLOW_PRIVATE_RANGES` is **cluster-wide**: it is read by every validation
call site. Allowlisting loopback for a local model backend also permits a
tenant-supplied connector `base_url` to target loopback on the same deployment.
Grant the narrowest CIDRs that work, and prefer a dedicated deployment when
untrusted tenants share a cluster with localhost services.

---

## Health Checks

Per-check timeout limits for `/healthz/ready` dependency probes. The global
value (`MODULO_HEALTH_TIMEOUT_SECONDS`) applies to every check unless a
per-check override is set to a positive value.

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `MODULO_HEALTH_TIMEOUT_SECONDS` | No | `5` | Global timeout for each readiness dependency check (seconds) |
| `MODULO_HEALTH_DB_TIMEOUT_SECONDS` | No | `0` | Database check timeout; `0` = use global |
| `MODULO_HEALTH_REDIS_TIMEOUT_SECONDS` | No | `0` | Redis check timeout; `0` = use global |
| `MODULO_HEALTH_CHECKPOINTER_TIMEOUT_SECONDS` | No | `0` | Checkpointer schema check timeout; `0` = use global |
| `MODULO_HEALTH_MIGRATIONS_TIMEOUT_SECONDS` | No | `0` | Alembic migration check timeout; `0` = use global |

A check that exceeds its limit reports `degraded` (redis/checkpointer/migrations)
or `unavailable` (database) with a "timed out after Ns" detail message instead of
blocking readiness indefinitely.

---

## Migration CLI

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `MODULO_ADMIN_SECRET` | For CLI | – | Shared secret for `modulo-migrate` CLI tool |
| `MODULO_ADMIN_TOKEN` | For CLI | – | Admin JWT for `modulo-migrate` CLI tool |

---

## Break-glass Admin Recovery

Operator-controlled emergency admin recovery for orgs whose only admin is
locked out (see `docs/prd.md` §7.19 and
`docs/operations/break-glass-admin-recovery-runbook.md`). The CLI connects to
the database as the dedicated `modulo_breakglass` role via
`MODULO_BREAK_GLASS_DATABASE_URL` – never the application `DATABASE_URL`.

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `MODULO_BREAK_GLASS_ENABLED` | No | from secret presence | Enable CLI `activate` + login-hook consumption. Deactivate/force/status stay operable while secrets + URL are present even when false |
| `MODULO_BREAK_GLASS_SECRET` | Yes (when ENABLED) | – | Primary operator secret; must differ from `_STANDBY_SECRET`, minimum length |
| `MODULO_BREAK_GLASS_STANDBY_SECRET` | Yes (when ENABLED) | – | Standby operator secret for rotation |
| `MODULO_BREAK_GLASS_TTL_MINUTES` | No | `1440` | Default credential TTL in minutes (min 1, ≤ `MODULO_BREAK_GLASS_MAX_TTL_MINUTES`) |
| `MODULO_BREAK_GLASS_MAX_TTL_MINUTES` | No | `4320` | Hard TTL cap (72h) |
| `MODULO_BREAK_GLASS_DATABASE_URL` | Yes (when ENABLED) | – | Dedicated `modulo_breakglass` role connection string (BYPASSRLS; never the app `DATABASE_URL`) |
| `MODULO_BREAK_GLASS_BOOT_FAILURE_MODE` | No | `warn` | `warn` or `fail` for URL/secret-presence checks; the allow-list/role assertions are FATAL in both modes |

Operational procedure: `docs/operations/break-glass-admin-recovery-runbook.md`.

---

## Full Example (.env)

```env
# Required
DATABASE_URL=postgresql+asyncpg://modulo:modulo@localhost:5434/modulo
SECRET_KEY=<random-64-char-string>
FERNET_KEY=<random-44-char-base64>

# Server
MODULO_PUBLIC_URL=https://modulo.example.com
CORS_ORIGINS=https://app.modulo.example.com,https://admin.modulo.example.com
CORS_MAX_AGE=3600
MODULO_LOG_LEVEL=INFO

# Redis (required for multi-replica)
REDIS_URL=redis://redis:6379/0

# Observability (optional)
MODULO_TELEMETRY_ENABLED=false
```

---

## Cross-Reference

| Topic | Document |
|-------|----------|
| System requirements | [`docs/system-requirements.md`](./system-requirements.md) |
| Deployment guide | [`docs/deployment.md`](./deployment.md) |
| Deployment security | [`docs/deployment-security.md`](./deployment-security.md) |
| Secret management | [`docs/security/secret-management.md`](./security/secret-management.md) |
| Backup & restore | [`docs/operations/backup.md`](./operations/backup.md) |
| Startup troubleshooting | [`docs/troubleshooting.md`](./troubleshooting.md) §1 |
