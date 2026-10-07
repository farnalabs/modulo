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
| `MODULO_SYSTEM_DATABASE_URL` | No | `""` | Dedicated connection string for cross-org system cron jobs (`modulo_system` role). When set, system crons use this instead of `DATABASE_URL`. |

`MODULO_DB=sqlite` switches to SQLite for local development (no RLS, no advisory locks, no flood protection).
`MODULO_DB=mariadb` or `mysql` uses the aiomysql driver. Both values are accepted by
the settings validator but are **not** production-ready: the per-node output
store (`db/crud/run_node_outputs.py::dialect_insert`) raises `NotImplementedError`
on any dialect other than `postgres` / `sqlite`, and MariaDB has been deprecated
since 2026-07-11. Use Postgres in production, SQLite for local development. See
[`docs/system-requirements.md`](./system-requirements.md) for the full backend
matrix.

### DB Capacity Monitor

Controls a 98% hard-stop that refuses new runs when database capacity is nearly full.

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `DB_CAPACITY_MODE` | No | `fixed` | Capacity mode: `fixed` (enforced), `elastic` (advisory only), or `disabled` |
| `DB_CAPACITY_BYTES` | No | `None` | Total capacity bytes for computing usage percentage (required when mode=fixed) |
| `DB_CAPACITY_BYPASS` | No | `false` | Operator bypass for the 98% hard-stop (e.g. deliberate migrations) |
| `DB_CAPACITY_HARD_STOP_PCT` | No | `98.0` | Percent of capacity at/above which new runs are refused (mode=fixed only) |

---

## Authentication & Secrets

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `SECRET_KEY` | **Yes** | – | JWT signing key, minimum 32 bytes (256 bits) |
| `FERNET_KEY` | **Yes** | – | Fernet encryption key, minimum 32 bytes (typically 44 for a standard Fernet key) |
| `FERNET_KEY_OLD` | No | – | Previous Fernet key for no-downtime rotation; decrypt falls back to this when `FERNET_KEY` is rotated |
| `MODULO_USERS` | For seeding | – | Comma-separated `user:pass` pairs for initial user seed |
| `MODULO_ADMIN_PASSWORD` | No | – | Admin password for single-admin alpha auth (at least one of `MODULO_ADMIN_PASSWORD` or `MODULO_USERS` must be set) |
| `MODULO_ADMIN_SECRET` | No | – | **CLI-only**. Shared secret for `modulo-migrate` CLI auth bypass (not part of the Settings class; read directly by the CLI tool) |
| `MODULO_ADMIN_TOKEN` | No | – | **CLI-only**. Admin JWT for `modulo-migrate` CLI (alternative to env; not part of the Settings class) |
| `MODULO_SECRETS_BACKEND` | No | `fernet` | Secrets backend: `fernet`, `vault`, or `aws` |
| `INVITATION_EXPIRY_HOURS` | No | `72` | How long an in-app invitation token stays consumable |

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

## Demo Mode

Optional visitor demo experience (FAR-535): navigating to `/demo` logs the visitor in as a known read-only demo user in a dedicated `Demo` organisation with benign sample data. All three variables must be set, otherwise the endpoint answers 404 and nothing is seeded.

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `MODULO_DEMO_ENABLED` | Yes (for demo) | `false` | Kill switch. Truthy (`true`/`1`) activates the `POST /api/v1/auth/demo` endpoint and the demo seed. |
| `MODULO_DEMO_USER` | Yes (for demo) | – | Email of the demo user account (created/updated idempotently at boot). |
| `MODULO_DEMO_PASSWORD` | Yes (for demo) | – | Password of the demo user. The seed re-stamps the stored hash to match on every boot, so rotating the secret takes effect on restart. |
| `MODULO_DEMO_TOKEN_MINUTES` | No | `120` | Demo access-token TTL in minutes. The demo session carries no refresh token and dies with this token. |

Point `MODULO_DEMO_USER` at a **dedicated** account: if it names an existing account, boot re-stamps that account's password to the demo password. Regardless, the demo endpoint only ever mints a session scoped to the `demo` organisation with the `viewer` role; an authenticating account without a viewer membership in the demo org (e.g. a privileged account) answers the same 404 as the kill switch.

The demo user gets a `viewer`-role membership (read-only; `is_system_admin` is forced off) and the seed is idempotent: it creates the `demo` organisation, the user, and benign sample data at boot, or immediately via `python -m modulo.db.seed_demo`: five schemas, six agents, four pipelines with editor-valid graphs (UUID node ids, first-class edges), including the Demo Governance Pipeline (Implement → PR risk level → Human review when risk > 0.50 → Open PR, with a conditional edge straight to Open PR when risk ≤ 0.50); twenty synthetic runs carrying per-node execution traces and costs, three triggers (including "Ticket ready" on the governance pipeline) and a "Delivery lifecycle" lifecycle map. Every boot converges existing demo rows to the current seed, so an upgrade repairs demo data seeded by an older release. Rate limiting: 10 requests/hour per IP on the demo endpoint.

---

## SSO / SAML 2.0

Team-tier feature (requires valid `MODULO_LICENSE_KEY`). Configurable via env vars or the admin SSO providers UI.

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `MODULO_OIDC_PROVIDERS` | No | `[]` | JSON array of `{provider_id, client_id, client_secret, discovery_url}` objects. **Deprecated**; use the admin SSO providers UI. |
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
| `MODULO_ACCESS_TOKEN_MINUTES` | No | `15` | Access token TTL in minutes (min 5, max 1440) |
| `MODULO_REFRESH_TOKEN_TTL_HOURS` | No | `24` | Refresh-token lifetime in hours (min 1, max 168). The refresh token slides on every rotation, so this is the idle-logout window: an abandoned session cannot refresh after it lapses. |
| `REFRESH_REUSE_GRACE_SECONDS` | No | `30` | Refresh-token reuse grace window in seconds (min 0, max 120). A refresh presented with a stale family sequence within this window after the most recent rotation is treated as a benign retry/replay rather than a theft signal, so the token family is not blacklisted. `0` disables the grace: any reuse blacklists the family immediately. |
| `DEBUG` | No | `false` | Enable debug mode (test/staging environments) |
| `MODULO_DEV_MODE` | No | `false` | Enable preview / in-development features |

---

## Redis & Task Queue

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `REDIS_URL` | No | `redis://localhost:6379/0` | `redis://host:port/db` for the SAQ broker and rate limiting |

Redis is **required** for production: the SAQ workers (runs + system) provide
run dispatch, cron firing, and the scheduler. Without Redis there is no
executor – only in-memory rate limiting and an in-memory event broker.

---

## Hosted Community Library

Client sync for the hosted community library of pipeline primitives.

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `MODULO_LIBRARY_ENDPOINT` | No | `""` | Library server endpoint URL. Empty disables library sync. |
| `MODULO_LIBRARY_ROOT_PUBLIC_KEY` | No | `""` | Ed25519 PEM public key for verifying signed library manifests |
| `MODULO_LIBRARY_SYNC_INTERVAL_SECONDS` | No | `300` | Seconds between library sync polls |
| `MODULO_LIBRARY_SYNC_TIMEOUT_SECONDS` | No | `15` | HTTP timeout for library sync requests |

---

## SAQ (task queue / workers)

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `SAQ_RUNS_QUEUE` | No | `runs` | Runs-queue name (`staging-runs` on staging for isolation) |
| `SAQ_HARD_GATE` | No | `true` | Healthz/ready 503-gates when the DEPLOYMENT has no live SAQ workers on a configured queue, or no fresh system-cron heartbeat anywhere in it (deployment-scoped after FAR-1158/ADR 043: not "this machine"; applied after the boot/probe grace). Set `false` to relax to alert-only: the condition is logged and alerted but never 503s readiness. The cutover deploy-hold was retired 2026-08-05 – this readiness gate is the only gate left |
| `SAQ_AUTH_PASSWORD` | Yes (system worker) | – | Fail-closed web UI auth password; refuse to boot without it |
| `SAQ_AUTH_USERNAME` | Yes (system worker) | – | Fail-closed web UI auth user; maps to the `AUTH_USER` env SAQ's web reads |
| `SAQ_RUN_RETRIES` | No | `5` | SAQ retries per run job – `N` is N total attempts (N-1 retries) |
| `SAQ_RETRY_DELAY` | No | `60` | Fixed retry delay in seconds (`retry_backoff=False`) |
| `SAQ_RUN_TIMEOUT` | No | `7200` | Per-run execution ceiling; the job must reach a terminal state within this budget (seconds) |
| `SAQ_RUN_CLAIM_CAP` | No | `20` | Per-claim cap on SAQ claim attempts for `dispatcher='saq'` runs |
| `SAQ_SETUP_GRACE_SECONDS` | No | `600` | Zombie-run protection: a run must dispatch at least one node within this window or the watchdog fails it |
| `SAQ_CLAIMED_NODELESS_MINUTES` | No | `35` | Secondary zombie net: a run still `running` with a fresh heartbeat but zero checkpoints after this many minutes is failed. Reduced from `45` by FAR-199 (bounds wedged-fleet accumulation); must stay above the 1800s max node timeout so a slow-but-healthy first node is never false-failed |
| `SAQ_NODELESS_EARLY_DETECT_MINUTES` | No | `15` | Early re-dispatch window (minutes) for a claimed-but-nodeless run whose heartbeat is still fresh: catches a wedged executor before the full `SAQ_CLAIMED_NODELESS_MINUTES` window elapses (min 5, max 120). Effective only when it is **below** `SAQ_CLAIMED_NODELESS_MINUTES`; at or above that value the early-detect branch is disabled with a load-time warning and the run waits the full window. Zero nodes have executed, so nothing double-executes |
| `SAQ_JOB_HEARTBEAT` | No | `300` | SAQ job heartbeat knob (per-job `heartbeat`) |
| `SAQ_REENQUEUE_WINDOW` | No | `600` | Re-enqueue staleness window for `dispatcher_reconcile` |
| `SAQ_NEVER_DISPATCHED_WINDOW` | No | `300` | Legacy never-dispatched sweep window (non-SAQ rows only) |
| `SAQ_WORKER_LOST_WINDOW` | No | `600` | Legacy worker-lost sweep window (non-SAQ rows only) |
| `SAQ_WORKER_DB_POOL_SIZE` | No | `65` | SAQ worker Postgres pool size (per worker). The effective pool is floored at `SAQ_WORKER_CONCURRENCY * 3 + 5` (max() in `saq_worker._effective_db_pool_size`; each concurrent run draws multiple DB connections, +5 reserve for the watchdog terminalize writes and shared system-cron connections). Operators on a small Postgres tier may lower it further. |
| `DB_POOL_RECYCLE_SECONDS` | No | `1500` | Recycle age (seconds) for pooled connections on the app's long-lived Postgres engines (the shared engine plus the system-role, reports, SAQ-hook and break-glass engines). MUST stay strictly below the HAProxy session window in front of Postgres (`timeout client/server 30m` = 1800 s in Fly's `/fly/haproxy.cfg`) — the field rejects values ≥ 1800 at load (fail-fast), so a pooled connection is always recycled by SQLAlchemy before the proxy can silently close it (FAR-1524; `pool_pre_ping` remains on as the checkout-time safety net). The 60 s floor exists because recycling more often than that buys nothing — connections already recirculate constantly under normal load — while adding needless reconnect churn; below ~60 s the churn cost dominates any benefit. |
| `SAQ_REDIS_POOL_SIZE` | No | `20` | SAQ Redis client pool size (Upstash connection budget). The effective pool is floored at `SAQ_WORKER_CONCURRENCY + 5` (max() in `saq_worker._effective_redis_pool_size`; blocking dequeue holds one connection per concurrent slot, +5 reserve for upkeep ops). Prod pins `50` in `fly.toml`; staging pins `10` in `deploy/fly/fly.staging.toml` (2026-09 Redis-usage audit right-size). Operators on a small Redis tier may lower it further. |
| `SAQ_WORKER_CONCURRENCY` | No | `5` | SAQ worker job concurrency, decoupled from Redis pool size. Design target 20/worker x up to 5 machines = up to 100 concurrent runs – verified-safe against the prod Postgres 300-connection cap (SAQ is asyncio single-engine, so concurrency does not multiply the DB pool). Prod pins `20` in `fly.toml` (ADR 017 design target); staging pins `2` in `deploy/fly/fly.staging.toml` (2026-09 Redis-usage audit right-size). |
| `RUN_CLAIM_STALE_SECONDS` | No | `450` | Staleness gate for re-claiming a SAQ run whose heartbeat is stale |
| `RUN_HEARTBEAT_SECONDS` | No | `30` | DB heartbeat cadence (keep below the 300s SAQ sweep threshold) |
| `SAQ_TEST_PAUSE` | TEST-ONLY | `false` | Test-only pause flag; refused outside test/staging (`DEBUG=true`) |
| `SAQ_NODE_DEFAULT_TIMEOUT_SECONDS` | No | `1200` | Default node execution timeout when graph node has no explicit timeout |
| `SAQ_NODELESS_REDISPATCH_BUDGET` | No | `4` | Max re-dispatch cycles for claimed-but-nodeless SAQ zombies (raised 2 → 4 by FAR-812 so a zero-node run survives a transient dispatch wobble) |
| `SAQ_CAPACITY_RETRY_BUDGET` | No | `3` | Per-run capacity-retry budget: a claimed run past this many total claims is terminal-failed regardless of TTL (min 0, max 20) |
| `HITL_REVIEW_CANCEL_GRACE_SECONDS` | No | `3600` | Instance layer of the three-level HITL review window (per-pipeline override > org default > instance default, FAR-1257): seconds after an open HITL gate expires unanswered before the gate is auto-cancelled, `cancelled` / `hitl_review_expired`, releasing the org concurrency slot (min 60, max 604800). The effective window is resolved once at gate fire time (pipeline override > org default > `HITL_CLAIM_TTL_SECONDS` (900) + this value, clamped to 60..604800) and stamped as the absolute `hitl_claims.terminalize_at`: org default via `GET/PUT /api/v1/admin/org/hitl-review-window` on `/admin/org` (reachable on every plan tier, FAR-1269), per-pipeline override in the pipeline editor. Stamped rows ignore this knob (the stamp is the deadline); it applies verbatim only to legacy unstamped rows. The deprecated name `HITL_GATE_CANCEL_GRACE_SECONDS` is still read when the new name is unset, with a rename warning logged at boot |
| `SLOT_RECONCILE_STALE_SECONDS` | No | `1800` | Stale heartbeat window for slot reconciliation sweep (force-releases leaked slots) |
| `HEARTBEAT_STALE_RETRY_BUDGET` | No | `3` | Heartbeat-stale auto-retry budget for the slot-reconcile sweep: a `running` run swept as heartbeat-stale is reset to `pending` for re-dispatch while its `claim_count` is ≤ this budget; only a claim beyond it terminal-fails (raised 1 → 3 by FAR-812 to absorb a transient dispatch wobble in a zero-node run) |
| `TRIGGER_BACKPRESSURE_MAX_AGE_SECONDS` | No | `3600` | Max age (seconds) for pending runs before trigger backpressure kicks in |
| `DISPATCHER_RECONCILE_BUDGET_SECONDS` | No | `95` | Per-tick time budget (seconds) for the dispatcher reconcile loop (min 10, max 119) |
| `DISPATCHER_RECONCILE_ORG_BUDGET_SECONDS` | No | `30` | Per-organisation time bound (seconds) inside the dispatcher reconcile loop (min 1, max 119): each org's reconcile pass is bounded by this many seconds, clamped to the tick's remaining budget minus a 15s tail reserve for facts/sweeps; on expiry the org's transaction rolls back, a truthful `status='timeout'` / `org_timeouts` marker naming the org is recorded, and the tick continues |
| `DISPATCHER_RECONCILE_TERMINALIZE_MAX_PER_TICK` | No | `25` | Per-tick row cap on the dispatcher reconcile terminalizer SQL (min 1, max 1000) |
| `DISPATCHER_RECONCILE_FACTS_MAX_PER_TICK` | No | `25` | Per-tick row cap on the dispatcher reconcile daily-facts compensator SQL (min 1, max 1000) |
| `DISPATCHER_RECONCILE_MAX_ROWS_PER_TICK` | No | `500` | Cross-org row budget per reconcile tick (min 50, max 10000): the TOTAL rows processed across all orgs (terminalisers + the reconcile scan) so one tick always finishes inside `DISPATCHER_RECONCILE_BUDGET_SECONDS`; overflow drains on subsequent ticks |
| `HITL_PARK_GRACE_SECONDS` | No | `86400` | Park-on-expiry grace (min 60, max 604800): seconds after the HITL review deadline before the run moves `awaiting_human` → `hitl_parked` (non-terminal, releases pipeline capacity; the gate stays open and claimable, park is not decide). Since FAR-1257 the park sweep anchors on the review deadline (`hitl_claims.terminalize_at`) plus one full park-grace window, floored at a 300s margin, so the terminalizing (cancel) sweep always acts first; legacy unstamped rows keep the `expires_at + grace` arithmetic |

`SAQ_HARD_GATE` replaces the removed `SAQ_ENABLED` flag: post-cutover SAQ is the
only dispatch path, so the readiness gate is always active. The deploy-time
`SAQ_HOLD` gate (deploy.yml `hold-check` job) was retired 2026-08-05 – no
deploy hold remains; `SAQ_HARD_GATE` is the only gate.

---

## Runner Capacity Gate

Controls runner-slot reservation for sandbox-agent dispatches (FAR-594 D8).

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `RUNNER_CAPACITY_GATE_ENABLED` | No | `false` | Enable the runner capacity gate. When ON, sandbox-agent dispatches reserve a runner slot through an atomic transaction. Tier-scoped default: when the org key is absent, the Docker-tier default gates Docker+Local dispatches only (E2B carries its own platform-side quota). |
| `RUNNER_CAPACITY_LOCK_TIMEOUT_MS` | No | `2000` | Lock timeout in milliseconds for the runner capacity gate transaction. SQLSTATE 55P03 degrades to a retryable capacity denial. Min 100, max 30000. |
| `RUNNER_MARKER_STALE_SECONDS` | No | `90000` | Stale threshold for runner dispatch markers (marker `written_at`, legacy tier-less fall back to `runs.updated_at`). Markers older than this are cleared by the reconciliation sweep. Min 3600, max 604800. |
| `RUNNER_MARKER_SWEEP_LOCK_TIMEOUT_SECONDS` | No | `5` | Lock timeout in seconds for the runner marker sweep transaction. Min 1, max 30. |
| `RUNNER_RECONCILER_DESTROY_ENABLED` | No | `false` | Enable the runner workspace orphan reconciler destroy path. When OFF (default), the reconciler runs in log-only soak mode and does not destroy orphaned workspace containers. |
| `MODULO_RUNNER_MACHINE_ID` | No | `""` | Deployment-identity label for the runner workspace reconciler. When empty, falls back to the machine hostname. Used to scope container orphan sweeps to a single deployment. |

---

## Pipeline Mutation Lock Timeout

Bounds the row-lock wait of a pipeline-MUTATION transaction (FAR-1279) - not a
runner knob: it is deliberately its own field so API PATCH contention stays
decoupled from runner-capacity tuning. The bound is applied transaction-scoped
(`SET LOCAL` semantics) before the lock is taken, so it covers the whole
mutation transaction, including the clone's separate-connection step-(a)
`FOR SHARE` read of the source row.

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `MUTATION_ROW_LOCK_TIMEOUT_MS` | No | `5000` | Lock timeout in milliseconds for the whole pipeline-mutation transaction (graph save, update, delete, clone, folder move, node conversion), not only the mutation row lock. Applied transaction-scoped (`SET LOCAL lock_timeout`) inside that transaction, so it also bounds every SUBSEQUENT lock wait the transaction takes after the row lock - the edge/index and FK locks of the graph write itself, and the per-organisation `audit_chain_heads` row lock taken by `append_audit_event` (contended by any concurrent audit append in the same org, not just by another pipeline mutation). Any of those waits expiring raises SQLSTATE 55P03, which degrades to HTTP 409 `Timed out waiting for a lock on this resource; another change is in progress. Re-issue the request once the other change completes.` - the mutation did NOT apply and is safe to re-issue - rather than an unbounded pooled-connection wait. Postgres only: the bound is dialect-gated and a no-op on sqlite/mariadb/mysql. Min 100, max 30000. |

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
| POST `/api/v1/auth/demo` | 10 | 3600s |
| POST `/api/v1/runs/{run_id}/hitl/{review_id}/{action}` and POST `/api/v1/runs/{run_id}/manual/{review_id}/submit` (review actions, both surfaces share one aggregate budget) | 20 per user, aggregate across runs/gates/actions (FAR-611) | 60s |

Additionally, the `AuthRateLimitMiddleware` enforces a separate login lockout:

| Path | Limit | Window |
|------|-------|--------|
| Auth endpoints (all POST/PUT/PATCH) | `MODULO_AUTH_MAX_ATTEMPTS` (default 10) | `MODULO_AUTH_WINDOW_SECONDS` (default 60s) |

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
| `MODULO_KUBERNETES_ENABLED` | For Kubernetes | – | Registers the `kubernetes`/`k8s` runtime provider when set to any value except an explicit negative (`0`, `false`, `no`, `off`); unset/falsy leaves it unregistered. In-cluster auth is used when `KUBERNETES_SERVICE_HOST` is set, otherwise the standard kubeconfig chain (`KUBECONFIG` / default path). |
| `MODULO_KUBERNETES_NAMESPACE` | No | `modulo` | Namespace the provider creates workspace pods in. |
| `MODULO_KUBERNETES_SERVICE_ACCOUNT` | No | `default` | ServiceAccount the workspace pods run under. |
| `MODULO_MAX_LOCAL_CONCURRENCY` | No | `2` | Max concurrent local agents (LocalRuntimeProvider) |
| `E2B_SANDBOX_USD_PER_HOUR` | No | `0.13` | Hourly USD rate for an E2B sandbox, used to estimate per-run agent runtime cost from wall-clock time; default reflects the opencode template (2 vCPU / 2 GiB) rate; set to your E2B sandbox rate. |
| `RUN_API_KEY_DEFAULT_TTL_SECONDS` | No | `900` | Per-run agent runtime API key TTL floor (min 300, max 86400) |
| `SANDBOX_PROVISIONING_TIMEOUT_SECONDS` | No | `90` | Timeout for sandbox workspace provisioning (min 15, max 600) |
| `SANDBOX_BINDING_RESOLVE_TIMEOUT_SECONDS` | No | `30` | Timeout for resolving runner bindings (decrypting referenced model-backend credentials) during dispatch (min 5, max 120) |

---

## Cost Tracking

Anti-abuse knobs for self-reported model cost. A violating value fails at
Settings load (fail-fast) – a bad env value blocks boot with a recovery message.

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `MODULO_MAX_REPORTABLE_USD_MIN` | No | `0.000001` | The floor: a self-reported `model_cost_usd` below this is NOT a report (closes the spend-evasion hole), except an EXACT `0` that the producer proves is genuine (all-zero `token_usage`, FAR-653), which IS a real report rendering `$0.00` with no warning. Sub-floor non-zero values stay rejected. `ge=0.000001` – a sub-floor knob is rejected. |
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
| `MODULO_AGENT_FAILURE_ELEVATION_ENABLED` | No | `true` | When a sandbox node reports agent_status=failed, terminalize the run as failed |
| `MODULO_IDEMPOTENCY_GATE_ENABLED` | No | `true` | Enable sandbox idempotency gate (re-run skip for single sandbox nodes) |
| `MODULO_CONNECTOR_WRITE_GATE_ENABLED` | No | `false` | Enable connector-write idempotency gate (skip byte-identical re-executed writes) |
| `MODULO_SEED_DEMO_ORGS` | No | `false` | Seed demo organisations with signed licenses on boot (gated framework) |
| `MODULO_PRODUCT_ANALYTICS_ENDPOINT_URL` | No | `""` | Endpoint URL for opt-in aggregate product analytics. Empty disables. |
| `MODULO_PRODUCT_ANALYTICS_INSTANCE_SECRET` | No | `""` | Instance secret for HMAC signing product analytics. Empty disables. |
| `MODULO_MONITOR_DOMAINS` | No | `""` | Space-separated CSP connect-src expressions (e.g. custom Grafana Faro collectors) |
| `MODULO_ARTIFACTS_ENABLED` | No | `true` | Enable pipeline artifact storage |
| `MODULO_ARTIFACTS_DIR` | No | `""` | Base directory for artifact files. Empty defaults to `<backend>/.data/artifacts` resolved by the store factory. |
| `MODULO_WORKSPACE_INPUTS_ENABLED` | No | `false` | Managed workspace inputs (MWI) kill-switch. When OFF (default), a `sandbox_agent` node that declares `workspace_inputs` short-circuits with `sandbox.workspace_inputs_disabled` and provisions nothing; existing sandbox behaviour is unchanged. Operators enable it explicitly once MWI is ready for production use |

---

## Organisation Settings (`settings_json`)

Per-organisation configuration is stored in the `settings_json` column of the
`organisations` table (not environment variables). Configured by an org admin
via the admin API. Unknown/absent keys default to safe values.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `sandbox_concurrency_limit` | `int` (1–100) or `null` | `null` (unlimited) | Max concurrently `running` sandbox-agent runs for the org across all pipelines. Runs beyond the cap stay `pending` with `error_code='org_capacity_limited'` and are retried by the background accelerator. Managed via `GET`/`PUT /api/v1/admin/org/sandbox-concurrency`. |
| `run_concurrency_limit` | `int` (1–100) or `null` | `null` (unlimited) | Max concurrently executing/claimed runs for the org across ALL pipelines (sandbox-agent and otherwise). Runs dispatched while the org is at this cap are deferred back to `pending` with `error_code='org_capacity_limited'` and retried by the background accelerator. Independent of `sandbox_concurrency_limit` – both are org-wide caps, and both produce the same `org_capacity_limited` marker on deferred runs. Managed via `GET`/`PUT /api/v1/admin/org/run-concurrency`. |
| `hitl_review_window_seconds` | `int` (60–604800) or `null` | `null` (instance default) | Org default HITL review window: how long a fired review may sit unclaimed/undecided before the run is terminalised `cancelled` / `hitl_review_expired`. A pipeline-level `hitl_review_window_seconds` wins over this; `null` clears the key and inherits the instance default. The terminaliser itself has no per-org opt-out and no "0 = disabled". Managed via `GET`/`PUT /api/v1/admin/org/hitl-review-window`, where an omitted field is a 422 and only an explicit `null` clears the org default |

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

The same variables also drive the Docker Compose **health watchdog**'s email
alerts (`watchdog` service in the root `docker-compose.yml`) – one SMTP setup
serves both. Watchdog alerting stays off until `SMTP_HOST`, `SMTP_PORT`,
`EMAIL_FROM` and `ALERT_EMAIL_TO` are all set; leaving them unset is a
supported state where monitoring still runs and only the emails are skipped.
See [`deployment.md` §Health watchdog](./deployment.md#health-watchdog-docker-compose).

The same SMTP setup also sends the **error-tracking**, **readiness** and **worker-liveness watchdog** alert emails, which identify the deployment environment and may carry operator-supplied context:

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `MODULO_ENV` | No | `development` | Deployment environment name. Applied to error-tracking events and structured logs, and shown as `Environment: <value>` on error-tracking alert emails (an empty value renders as `N/A`) and on [readiness health](#readiness-health-alerts) / [worker-liveness watchdog](#worker-liveness-watchdog) alerts (an empty value renders as `unknown`). The Compose deployment sets it to `production` (`deploy/compose/docker-compose.prod.yml`) and the Helm chart defaults it to `production` (`deploy/helm/modulo/templates/configmap.yaml`). |
| `ALERT_CONTEXT` | No | – | Operator-supplied free text appended verbatim to every readiness and [worker-liveness watchdog](#worker-liveness-watchdog) alert – runbook links, escalation notes, ticket pointers. One item per line; blank lines are dropped and the rendering is bounded (at most 20 lines, 300 chars each, the environment line always first). Carried on email, generic webhook and Teams channels alike. Never written to logs (the setting is `repr=False`). |

The readiness and [worker-liveness watchdog](#worker-liveness-watchdog) emails, webhooks and Teams messages carry the environment line and `ALERT_CONTEXT`; the out-of-process Gatus sentinel and the error-tracking pipeline do not use `ALERT_CONTEXT`.

### Readiness health alerts

The same SMTP configuration also drives the system worker's in-app readiness alerting: when **both** `SMTP_HOST` and `ALERT_EMAIL_TO` are set, the `health_readiness_alert` cron (every 5 minutes) evaluates health through the same checks as `/healthz/ready` and emails `ALERT_EMAIL_TO`:

- **One alert email** when readiness *confirmedly* transitions into `degraded` or `unavailable` – the email lists each failing sub-check with its detail.
- **One recovery email** when readiness returns to `ok`, so an incident has a visible end.

Notifications are edge-triggered and deduplicated: a new state must hold for 2 consecutive ticks (a ~10-minute worst-case notification latency, a single-probe blip never emails), and the dedup state is persisted in Redis so there is **one email per incident, never one per tick**. With no SMTP configuration (the compose deployment default), the cron still runs and evaluates health, never errors, and logs at most once per hour that alerting is disabled – quiet, not silent.

This covers degradation *while the app is up*. A **full outage** is covered separately by the compose deployment's external [Gatus health watchdog](./deployment.md#health-watchdog-docker-compose), which does not depend on Modulo itself running.

---

## Worker Liveness Watchdog

An in-process asyncio task running in the web-process FastAPI lifespan that reads SAQ worker liveness from Redis every tick and fires alerts when all workers are dead. No alert is sent until at least one alert channel is configured.

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `WATCHDOG_ENABLED` | No | `true` | Enable the watchdog tick |
| `WATCHDOG_TICK_SECONDS` | No | `30` | Tick interval in seconds |
| `WATCHDOG_WORKER_STALE_SECONDS` | No | `180` | Sustained window before an alert fires - applies to BOTH the SAQ-worker liveness condition AND the system-cron (`fire_due_triggers`) heartbeat condition, each of which must look bad continuously for this many seconds |
| `WATCHDOG_ALERT_STATE_TTL_SECONDS` | No | `604800` | Edge-triggered alert state TTL (default 7 days) |
| `ALERT_WEBHOOK_URL` | No | – | Slack-compatible webhook URL for watchdog alerts |
| `ALERT_TEAMS_WEBHOOK_URL` | No | – | Microsoft Teams incoming webhook URL |
| `ALERT_EMAIL_TO` | No | – | Comma-separated email recipients for watchdog alerts and readiness-degradation alerts (see [Readiness health alerts](#readiness-health-alerts)) |

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
| `MODULO_CSRF_EXEMPT_PATHS` | No | `/api/v1/health,/api/v1/triggers,/api/v1/auth,/api/v1/webhooks` | Comma-separated paths exempt from CSRF |

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
cloud-metadata ranges are a **non-negotiable floor**: no allowlist entry can
make them reachable.

### Self-hosted targets on localhost

Reaching a service on the host (a local Ollama / vLLM / LM Studio model backend,
or a connector left on its localhost default) requires an explicit opt-in:

```bash
SSRF_ALLOW_PRIVATE_RANGES=127.0.0.0/8,::1/128
```

**Both entries are required.** `localhost` resolves to `127.0.0.1` *and* `::1` on
a dual-stack host, and validation fails closed if any resolved address is
blocked, allowlisting only `127.0.0.0/8` leaves `http://localhost:11434`
unreachable. Alternatively, use a literal `http://127.0.0.1:11434` URL, which
skips DNS resolution entirely and needs only `127.0.0.0/8`.

These connectors ship a localhost default `base_url`, so they need the opt-in
above (or an explicit non-loopback `base_url`) before they will connect:

| Connector | Default `base_url` |
|-----------|--------------------|
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
| `MODULO_HEALTH_DB_HYGIENE_TIMEOUT_SECONDS` | No | `1` | Database-hygiene check timeout (seconds); `0` = use global |
| `MODULO_HEALTH_DB_HYGIENE_MIN_DEAD_TUPLES` | No | `10000` | Database-hygiene: absolute dead-tuple floor - a table's dead-tuple ratio is only acted on once it carries at least this many dead rows (minimum `0`; `0` considers every table, including zero-size relations) |
| `MODULO_HEALTH_DB_HYGIENE_DEAD_RATIO` | No | `0.60` | Database-hygiene: worst-table dead-tuple ratio at/above which a table over the floor grades `degraded` (`0`–`1`) |

A check that exceeds its limit reports `degraded` (redis/checkpointer/migrations) or
`unavailable` (database) with a "timed out after Ns" detail message instead of blocking
readiness indefinitely.

The database-hygiene probe is the exception to that wording (FAR-1510). When it does not
complete within `MODULO_HEALTH_DB_HYGIENE_TIMEOUT_SECONDS` it reports `degraded` with the
detail "database-hygiene probe did not complete within Ns (likely an event-loop stall; see
event_loop_lag) – hygiene not measured", and that result is ADVISORY: it stays visible in
the readiness body but is excluded from the aggregate gate, so on its own it neither flips
`/healthz/ready` to `degraded` nor fires the `health_readiness_alert` email. Nothing was
measured, so there is no hygiene verdict to act on – the stall itself is what the advisory
`event_loop_lag` check reports.

The database-hygiene sub-check's two thresholds (`MODULO_HEALTH_DB_HYGIENE_MIN_DEAD_TUPLES`,
`MODULO_HEALTH_DB_HYGIENE_DEAD_RATIO`) are grading settings, not timeouts: the check
reports the worst table from `pg_stat_user_tables` plus this database's freeze age
against `autovacuum_freeze_max_age`, and grades `degraded` (never `unavailable`, so it
never 503s readiness on its own) when either threshold is breached. A completed reading
that grades `degraded` DOES gate the aggregate – only a probe that never finished is
advisory.

---

## Migration CLI

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `MODULO_ADMIN_SECRET` | For CLI | – | Shared secret for `modulo-migrate` CLI tool |
| `MODULO_ADMIN_TOKEN` | For CLI | – | Admin JWT for `modulo-migrate` CLI tool |

---

## Break-glass Admin Recovery

Operator-controlled emergency admin recovery for orgs whose only admin is
locked out (see
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
