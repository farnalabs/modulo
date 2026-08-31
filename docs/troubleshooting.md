# Troubleshooting Guide

Common issues, their causes, and resolutions.

---

## 1. Startup Failures

| Symptom | Cause | Resolution | Log Pattern |
|---|---|---|---|
| `SECRET_KEY not set` | Missing env var | Set `SECRET_KEY` (minimum 32 bytes) | `ValidationError: SECRET_KEY must be at least 32 bytes` |
| `FERNET_KEY not set` | Missing env var | Set `FERNET_KEY` (base64-encoded, at least 32 bytes) | `ValidationError: FERNET_KEY must be at least 32 bytes` |
| `Fernet key must be 32 url-safe base64-encoded bytes` | `FERNET_KEY` is long enough to pass the startup length check but does not decode to exactly 32 bytes | Regenerate the key with `python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` (a 44-character value) | `ValueError: Fernet key must be 32 url-safe base64-encoded bytes.` |
| `Cannot connect to Postgres` | DB not running, wrong `DATABASE_URL`, or network issue | Check `docker compose ps`, verify `DATABASE_URL` is correct, ensure Postgres is accepting connections | `sqlalchemy.exc.OperationalError: could not connect to server` |
| `Alembic migration failed` | Version mismatch, branch migration, or `VARCHAR(32)` column width | Check `alembic_version` table exists with `VARCHAR(255)` for branch IDs; run `uv run alembic upgrade heads` | `alembic.util.exc.CommandError` or `psycopg2.errors.StringDataRightTruncationError` |
| `Redis connection refused` | Redis not running or wrong `REDIS_URL` | Check `docker compose ps`, verify `REDIS_URL` | `redis.exceptions.ConnectionError: Error 10061` |
| `Address already in use` | Port conflict (another process on the same port) | Change port via env vars or kill conflicting process (`netstat -ano \| findstr :PORT`) | `OSError: [Errno 10048] error while attempting to bind on address` |

---

## 2. Authentication Issues

| Symptom | Cause | Resolution | Log Pattern |
|---|---|---|---|
| `401 Unauthorized` on every request | Invalid, expired, or malformed JWT | Re-login with `POST /api/v1/auth/login` to obtain a fresh token | `401: Token has expired` or `401: Invalid token` |
| `403 Forbidden` | User role lacks required permission | Check user role (`admin`/`operator`/`runner`/`viewer`); upgrade role via admin API if needed | `403: Insufficient permissions` |
| Login succeeds but no data returned | No organisation has been created | Create an org via the admin API (`POST /api/v1/admin/orgs`) | No error – empty responses from all API calls |
| `Invalid API key` | Wrong key, expired, or revoked | Create a new API key in admin settings; verify the key prefix matches the expected pattern | `401: Invalid API key` |
| SSO login redirect fails | OIDC/SAML provider misconfiguration | Check provider settings (client ID, client secret, discovery URL); verify `redirect_uri` matches the provider's allowlist | `OIDCError: redirect_uri_mismatch` |

---

## 3. Pipeline Execution Issues

| Symptom | Cause | Resolution | Log Pattern |
|---|---|---|---|
| Run stuck in `running` status | Node timeout, LLM not responding, or cancelled node never fires | Check `@cancellable_node` timeout configuration; verify model backend is configured and reachable | `Node <name> exceeded timeout of <N>s` |
| Run fails with `LLM_TIMEOUT` | Model backend not responding within configured timeout | Check model backend credentials and connectivity; increase timeout in agent settings | `LLM_TIMEOUT: Model backend <name> did not respond within <N>s` |
| Run fails with `CONNECTOR_ERROR` | Connector auth failure or network issue | Check connector credentials in settings; verify the connector's target service is accessible from the backend host | `CONNECTOR_ERROR: <connector_name>: <error_details>` |
| Run fails with `VALIDATION_ERROR` | Agent output doesn't match expected schema | Check agent output against the assigned schema; fix prompt to produce conformant output | `VALIDATION_ERROR: Schema <name>: <validation_errors>` |
| `Graph validation failed` | Pipeline topology is invalid | Check edge connections for compatibility (node types, schema matching) | `GraphValidationError: <reason>` |
| Run stuck in `pending` with `error_code` `pipeline_capacity` or `org_capacity_limited` | The pipeline's `max_concurrent_runs` or one of the org's concurrency caps (`sandbox_concurrency_limit` for sandbox-agent runs, `run_concurrency_limit` for all org runs) was reached. Capacity-blocked runs stay `pending` (with a reason marker on `error_code`) and are retried in the background; they are NOT rejected at POST time | Wait for an active run to complete so a slot frees – the background accelerator admits the run automatically. If it stays blocked beyond the TTL it terminal-fails with `capacity_timeout`. Raise the pipeline limit; raise the sandbox-agent cap via `PUT /api/v1/admin/org/sandbox-concurrency`; or raise the org-wide run cap via `PUT /api/v1/admin/org/run-concurrency` | `pipeline_capacity` / `org_capacity_limited` / `capacity_timeout` |

---

## 4. HITL Issues

| Symptom | Cause | Resolution | Log Pattern |
|---|---|---|---|
| Gate not appearing in UI | Pipeline has `human_only` flag set, preventing automatic show | Check `human_only` flag on the pipeline – these gates require explicit human review and won't auto-proceed | Pipeline remains in `awaiting_human` state |
| `Claim token expired` | 15-minute TTL exceeded since claim | Re-claim the gate – a new claim token is auto-generated | `401: Claim token expired` |
| `409 Conflict on claim` | Another user already claimed this gate | Wait for the other user to complete their review or for the claim token to expire (15 min) | `409: Gate <id> already claimed by user <name>` |
| Cannot approve/reject | Claim token is invalid, expired, or gate already decided | Refresh the page; re-claim the gate if needed | `401: Invalid claim token` or `409: Gate already decided` |
| `human_only gate blocked` | Pipeline has `human_only: true` and requires a human-in-the-loop | This is by design – use the UI or MCP `review_hitl` tool to review; auto-approval is not possible | `human_only gate <id> is blocked – manual review required` |

---

## 5. WebSocket/Event Issues

| Symptom | Cause | Resolution | Log Pattern |
|---|---|---|---|
| WebSocket disconnects frequently | Network issues, proxy timeout, or load balancer idle timeout | Check reverse proxy WebSocket support (e.g. `proxy_read_timeout` in nginx); increase timeout configuration | `WebSocket disconnected: code 1006` |
| Events not updating in UI | WebSocket disconnected or event broker ring buffer full | Refresh the page; reconnect will replay from the last event sequence number | Missing live updates on run inspection |
| Replay events not working | Requested `since_event_seq` is outside the 100-event ring buffer range | Use a lower sequence number or perform a full reconnect (omit `since_event_seq`) | Evicted events replay as an empty list; `since_event_seq < 0` closes the socket with WS code 4001 |

---

## 6. Webhook Issues

| Symptom | Cause | Resolution | Log Pattern |
|---|---|---|---|
| Webhook not firing | Endpoint auto-disabled after repeated failures | Re-enable the endpoint in notification settings; check endpoint availability | `Endpoint <url> disabled after <N> consecutive failures` |
| HMAC validation failing | HMAC secret mismatch between sender and receiver | Rotate the HMAC secret in notification settings and update the receiver | `HMAC signature mismatch` |
| Duplicate webhook calls | Retry mechanism delivering the same event multiple times | Check the delivery log for retry count; dedup is content-hash based (SHA-256 of the raw payload in `webhook_dedup_hashes`) so identical payloads collapse into one run | Multiple delivery log entries for the same payload hash |
| `Flood protection triggered` | Too many identical webhooks in a short window | Check deduplication configuration; verify the webhook source is not sending duplicate payloads | `429: Flood protection – too many identical webhooks` |

---

## 7. Performance Issues

| Symptom | Cause | Resolution | Log Pattern |
|---|---|---|---|
| Slow pipeline execution | LLM latency, connector latency, or resource contention | Check p50/p95/p99 duration in run stats; review model backend health; consider switching to a faster model | High `duration_ms` in run inspection |
| High memory usage | Too many runs held in memory simultaneously | Reduce `max_concurrent_runs`; review checkpoint cleanup settings | OOM-killer events or rising RSS in container metrics |
| Slow API responses | DB query performance or missing indexes | Check slow query log; review index strategy (add missing indexes on frequently-queried columns) | Queries exceeding 100ms in slow query log |
| UI feels sluggish | Large pipeline graphs, excessive WebSocket events, or browser memory pressure | Reduce pipeline complexity (fewer nodes/edges); check browser console for JS errors or memory warnings | Browser DevTools Performance tab showing long frame times |

---

## 8. Known Limitations

- **SQLite mode**: No RLS enforcement, no advisory locks, no flood protection. Development only – not for production.
- **Claim tokens**: Single-use with a 15-minute TTL. Expired tokens cannot be refreshed – re-claim the gate.
- **WebSocket ring buffer**: Limited to 100 events per run. Older events are not available for reconnect replay.
- **Postgres required for production**: SQLite is development-only. Postgres is the only supported production database. See [`docs/system-requirements.md`](./system-requirements.md).
- **File upload limits**: Library `.zip` imports cap at 50 MB; guardrail payloads at 1 MB. Webhook deliveries are not capped at 10 MB.
- **Concurrent runs**: Capacity blocks (`max_concurrent_runs` and org caps) demote excess runs to `pending` and retry them in the background; they are not rejected with a 429 at POST time.
- **API key scoping**: Keys are scoped to `operator` and `runner` roles only. Admin operations require JWT auth.

---

## 9. Runtime Error Code Runbook

Every run-level failure is terminal-failed with a named `error_code`. This runbook maps each code to what it means, what recovery happens **automatically** (the autonomous pipeline: dispatcher_reconcile every 60s, the stale-run sweep every 5 min, the SAQ retry/claim machinery), and what a human should do.

| Error code | Meaning | Automatic recovery | Human action |
|---|---|---|---|
| `executor_stalled` | A claimed SAQ run dispatched **no node** within the setup grace (the `execute_run` zombie watchdog at `SAQ_SETUP_GRACE_SECONDS`), or `dispatcher_reconcile` found a claimed-but-nodeless zombie (fresh heartbeat, zero LangGraph checkpoints after `SAQ_CLAIMED_NODELESS_MINUTES`). | Two paths behave differently. The setup-grace zombie watchdog terminal-fails only. The `dispatcher_reconcile` nodeless path re-dispatches first (safe – zero nodes executed, so nothing can double-execute): bounded by the pipeline's `retry_policy` ("stall" in `on` → `max_retries`; a non-empty policy without "stall" → terminal-fail; no/empty policy → `SAQ_NODELESS_REDISPATCH_BUDGET`, default 2), throttled to at most one re-dispatch per `SAQ_CLAIMED_NODELESS_MINUTES` window per run, then terminal-failed once the budget is exhausted. | Investigate the worker machine that claimed the run (wedged worker? pre-node hang in checkpointer/graph compile/connector init?). Check `saq:runs:stats` heartbeats for that host. No user action on the run itself – it is already terminal. |
| `nodeless_zombie` | Legacy / reserved constant in `db.crud.run`; the live nodeless terminalizer writes `executor_stalled` instead. Retained so the two names cannot drift. | – | Not emitted by current code; see `executor_stalled`. |
| `executor_setup_failed` | `load_and_setup` raised before any node could run (checkpointer setup, graph compile, connector/model-backend hub init, or a DB error). | `execute_run` catches it and terminal-fails the run instead of leaving it `running` forever. SAQ may retry per job retries. | Check backend logs for the setup traceback. Often transient (DB blip) – a manual re-trigger via the UI/MCP may succeed. |
| `executor_failed` | The executor's `execute` loop failed (agent/node execution error not covered by a more specific code). | Run terminal-failed; SAQ retries per job retries. | Inspect logs; fix the underlying cause (model backend health, connector credentials). |
| `executor_heartbeat_lost` | The DB heartbeat loop failed **fail-closed** (3+ consecutive heartbeat writes failed) while the run was mid-execution. | Run terminal-failed – the run is not left `running` with no live writer. | Check DB connectivity from the worker machines; investigate why heartbeats stopped writing. |
| `executor_superseded` | The run was superseded – a successor claim rotated the `claim_token`, or the sandbox dispatch marker was denied. The superseded executor's fenced writes (heartbeat, terminalize) become no-ops. | The successor continues the run; the superseded executor aborts. Recovery/resume is working as designed. | Normally **no action**. Investigate only if runs churn through supersession repeatedly (a claim-cap or re-dispatch storm). |
| `node_cancelled` | A node was cancelled – `cancellation_requested`, per-node timeout, or the node retry budget exhausted. | Retryable node-failure classes propagate; the run fails or retries per the node's retry policy. | Expected when a user cancels a run. For timeouts, raise `node_timeout_seconds` on the pipeline. |
| `claim_cap_exhausted` | A running SAQ run at `claim_count >= SAQ_RUN_CLAIM_CAP` whose heartbeat went **stale** – nothing claimed it (a live run on its final claim is never killed; the gate is stale-heartbeat-only). | `dispatcher_reconcile` terminal-fails it. | Investigate why claims stopped (worker dead, queue wedge, Redis pool starvation – see the "silent wedge" lessons). |
| `dispatch_failed` | An enqueue-failed run whose marker (`enqueue_failed_at`) is older than the TTL backstop (60 min) **and** Redis is verifiably reachable. | `dispatcher_reconcile` re-dispatches enqueue-failed runs on a bounded interval; terminal-fails only past the backstop with Redis reachable. Redis down → the run stays `pending` (deferred). | Check Redis/queue health. The run was left pending for up to 60 min, then failed – a long Redis outage window is the usual cause. |
| `enqueue_failed` | Dispatch-time SAQ enqueue failure – the run is left `pending` with `enqueue_failed_at` stamped (never terminal-failed at dispatch time). | `dispatcher_reconcile` re-dispatches it on the bounded interval, capped at 50/tick. | Only investigate if a run stays `pending` past the backstop – that points at Redis/queue unavailability. |
| `capacity_timeout` | A capacity-blocked run (org or pipeline cap) sat `pending` past its TTL without a slot freeing. | The stale-run sweep terminal-fails it after the TTL. | Raise the relevant concurrency cap (`max_concurrent_runs`, `sandbox_concurrency_limit`, `run_concurrency_limit`) or wait for active runs to complete. |
| `org_capacity_limited` | **Non-terminal marker** (not a failure): the org's `run_concurrency_limit` / `sandbox_concurrency_limit` was reached; the run was demoted back to `pending`. | Auto re-dispatch when a slot frees – the 60s `dispatcher_reconcile` is the fast path, the stale-run sweep the durable backstop. | If sustained, raise the org cap via the admin endpoints. |
| `pipeline_capacity` | **Non-terminal marker** (not a failure): the pipeline's `max_concurrent_runs` was reached; the run demoted to `pending`. | Auto re-dispatch when a slot frees. | Raise `max_concurrent_runs` if the pipeline is chronically at capacity. |
| `never_dispatched` | A run was never claimed within the never-dispatched window (legacy sweep, `SAQ_NEVER_DISPATCHED_WINDOW`). | The stale-run sweep terminal-fails it. | Investigate why dispatch never happened (queue/worker down at creation time). |
| `worker_lost` | A run accumulated 5+ claims without reaching completion (legacy `worker_lost` sweep, `SAQ_WORKER_LOST_WINDOW`). | The stale-run sweep terminal-fails it. | Investigate repeated claim failures (executor crashing at startup, claim/demote churn loop). |

Monitoring surfaces for all of the above: `/healthz/ready` exposes the `dispatcher_reconcile` counters (`claim_cap_terminalized`, `age_terminalized`, `nodeless_failed`, `dispatch_failed_terminalized`, `enqueue_failed_*`, ...) and the `stale_run_recovery` advisory check; when telemetry is enabled the OTel metrics `runs_running_count`, `runs_oldest_running_age_seconds`, `runs_stall_reason_total`, and `runs_claim_count_total` are updated every reconcile tick.

### Analytics facts for raw-terminalised runs (FAR-162 / P6')

Every raw terminal writer that bypasses `finalize_cost` records a compensating
`run_daily_facts` row through the shared `record_fact_for_terminal_failed_run`
wrapper (fail-open, its own separate RLS-scoped session, idempotent upsert on
`run_id`). This covers: the SAQ `task_failure` hook, the stale-run sweep
(`never_dispatched` / `capacity_timeout` / `worker_lost`), `dispatcher_reconcile`
(`executor_superseded` / `claim_cap_exhausted` / `dispatch_failed`) and
`fail_run_terminal` (`executor_stalled` / `executor_heartbeat_lost` /
`executor_failed` / `executor_setup_failed`). A run failing through any of these
paths is therefore visible in the analytics failure/stall dimensions. The
writes are best-effort: a facts failure is logged and swallowed, never rolled
back against (or propagated from) the already-committed terminal status write.

### Synthetic `error_detail` on the sweep writers and the hang-death detector (FAR-164)

The `never_dispatched` and `worker_lost` sweep writers stamp a synthetic
`error_detail` ("Run was not dispatched within the stale threshold." /
"Worker lost heartbeat for this run.") so the runs list / detail view always
has something to show for these genuinely detail-less failures. This is safe
for the daily-watcher **hang-death detector**
(`Repos/devtools/dogfood/pipeline-scripts/_hang_deaths.py`): it keys on
`error_code == "node_cancelled"` ONLY – `worker_lost` / `never_dispatched` are
never `node_cancelled`, so adding a string detail to them can never be
miscounted as a hang death. Do NOT "fix" the detector to count these codes –
they are dispatch/harness failures, not sandbox-agent hang deaths. Note the
detector's `detail_available` page flag is now near-permanently `True` anyway
(P6' writes string detail for `task_failure` runs), so the effective gate for a
hang-death count is the `"likely hung"` marker in the detail of a
`node_cancelled` run.

---

## Log Locations

| Environment | Log Source | Location |
|---|---|---|
| Docker (local) | Backend stdout | `uv run uvicorn modulo.api.main:app --reload --port 8000` (terminal that runs the backend) |
| Docker (local) | Postgres | `docker compose -f docker-compose.local.yml logs -f db-local` |
| Docker (local) | Redis | `docker compose -f docker-compose.local.yml logs -f redis-local` |
| Production | Backend (JSON structured) | `journalctl` or log file per deployment config |
| Production | Postgres slow query log | `postgresql-<date>.log` (configurable via `log_min_duration_statement`) |
| Production | Nginx/Ingress | Access and error logs per ingress controller |
