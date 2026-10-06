---
id: feat-infra-health
prd: N/A
adr: [ADR 021 (worker-resilience)]
delivery-tasks: []
code:
  - backend/src/modulo/api/routes/health.py
  - backend/src/modulo/core/health_alerts.py
  - backend/src/modulo/core/alert_context.py
  - backend/src/modulo/core/watchdog/worker_liveness.py
  - backend/src/modulo/core/saq_worker.py
  - deploy/watchdog/config.yaml
  - fly.toml
  - .github/workflows/uptime-monitor.yml
unit-tests:
  - backend/tests/unit/api/test_health.py
  - backend/tests/unit/core/test_health_alerts.py
  - backend/tests/unit/core/test_alert_context.py
  - backend/tests/unit/core/watchdog/test_worker_liveness.py
  - backend/tests/unit/test_watchdog_config.py
  - backend/tests/docker/test_watchdog_container.py
bdd:
  - backend/tests/bdd/features/infra/health.feature
  - backend/tests/bdd/features/infra/test_health_steps.py
depends-on: []
status: covered
---

# Health Checks

Liveness and readiness endpoints for deployment health monitoring, plus the production
uptime watchdog that alerts on outage (FAR-400), plus three alerting legs: the
`health_readiness_alert` system-cron email (FAR-1446), the in-process worker-liveness
watchdog (ADR 021 worker-resilience) and the compose deployment's out-of-process Gatus
sentinel (PR #1260). Every operator alert — the readiness cron and the watchdog, across
email, generic webhook and Teams — identifies the deployment environment and carries the
operator's `ALERT_CONTEXT` free text through one shared renderer (`core/alert_context.py`,
FAR-1495 / FAR-1499). Liveness (`/healthz`) is advisory — it never flips readiness.
Readiness (`/healthz/ready`) aggregates database, Redis, checkpointer schema, Alembic
migration status, database hygiene (dead-tuple bloat + wraparound age, when the probe
completes), worker/cron/scheduler liveness and returns 503 whenever any gate is
unavailable. The AI agent can also be redirected to this infra-health surface via
`feat-infra-health`.

## Behaviours

- [x] `GET /healthz` returns `{"status": "ok"}` and is advisory only (never flips readiness)
- [x] `GET /healthz/ready` checks database connectivity (SELECT 1)
- [x] Redis connectivity check (degraded when not configured)
- [x] Checkpointer schema accessibility check (degraded on failure)
- [x] Alembic migration status check (degraded when migrations are pending)
- [x] Database-hygiene sub-check (FAR-1445) — worst-table dead-tuple ratio
      (`n_dead_tup / (n_live_tup + n_dead_tup)` from `pg_stat_user_tables`, over a
      configurable dead-tuple floor, zero-size relations excluded from the pick) plus
      `age(datfrozenxid)` against the server's own `autovacuum_freeze_max_age`;
      degrades (never `unavailable`, so never 503s readiness alone) on bloat at/above
      the configured ratio or on freeze age past 50% of the ceiling; thresholds via
      `MODULO_HEALTH_DB_HYGIENE_MIN_DEAD_TUPLES` / `MODULO_HEALTH_DB_HYGIENE_DEAD_RATIO`
- [x] A database-hygiene probe that does NOT complete within its budget (timeout, or the
      probe raising) reports `degraded` — "database-hygiene probe did not complete within
      Ns (likely an event-loop stall; see event_loop_lag) — hygiene not measured" — but is
      ADVISORY (FAR-1510): still listed in the body, never gating the aggregate and never
      firing the readiness alert, because no hygiene reading was taken. Only a completed
      reading graded over a threshold gates.
- [x] SAQ worker liveness check — a stopped worker pool for 4+ consecutive probe ticks 503s readiness (Plan F7)
- [x] System-cron liveness watchdog — fire_due_triggers missing 2x cadence 503s readiness (Plan F8)
- [x] Stale-run recovery sweep outcome — ADVISORY, never gates readiness: a missing or
      >15min-stale sweep reports `degraded` to alert operators while the app stays
      healthy (`_check_stale_run_recovery`, health.py)
- [x] Dispatcher reconcile staleness — two tiers: `degraded` after a single missed 60s tick
      is advisory (never flips readiness), `unavailable` past 5 minutes — the system
      worker's cron is silently dead and the fleet can no longer terminalize
      stalled/never-dispatched runs — 503s readiness
- [x] Fleet worker / fleet system-cron aggregation (worker process-group health, ADR 021)
- [x] Break-glass watchdog exposure is advisory and never contributes to readiness
- [x] Per-check timeout limits, configurable via `modulo_health_*_timeout_seconds` settings
- [x] Overall status: `unavailable` if any gate is `unavailable`, `degraded` if any gate
      is `degraded`. Gates are `database`, `redis`, `checkpointer`, `migrations`,
      `saq_workers`, `system_crons`, a COMPLETED `db_hygiene` reading, and
      `dispatcher_reconcile` at its `unavailable` tier only. Advisory checks are listed
      in the body but excluded from the aggregate: `break_glass`, `event_loop_lag`,
      `stale_run_recovery`, `slot_reconciliation`, `hitl_park_sweep`,
      `runner_workspace_reconcile`, `runner_marker_sweep`, `runner_health_probe`, the
      `degraded` tier of `dispatcher_reconcile`, and a database-hygiene probe that did
      not complete (FAR-1510). Source of truth: `evaluate_readiness` in health.py
- [x] 503 status code when overall unavailable
- [x] Latency tracked per check
- [x] Fly.io deployment wiring — `fly.toml` `[[http_service.checks]]` probes `/healthz/ready`
- [x] Worker process-group health check via top-level `[checks]` (ADR 021)
- [x] Production uptime monitor — `.github/workflows/uptime-monitor.yml` probes
      `app.modulo.run/healthz/ready` every 10 minutes and fails + opens a ticket on outage
- [x] Readiness-degradation email alert (FAR-1446): the `health_readiness_alert`
      system cron runs every 5 minutes and emails `ALERT_EMAIL_TO` when readiness
      CONFIRMEDLY transitions into `degraded`/`unavailable` (edge-triggered with a
      2-tick `CONFIRM_TICKS` hysteresis, so a one-tick blip/flap never emails),
      then sends one matching recovery email when it returns to `ok` — never one
      per tick. It evaluates the SAME `evaluate_readiness` code the HTTP route
      runs (no reimplemented checks) and keeps its dedup/confirmation state in
      Redis (one JSON doc, `STATE_TTL_SECONDS` 7 days; NOT the database, since a
      down database is one of the states being alerted about), send-then-commit
      so a failed SMTP send retries next tick. Quiet (never raises, hourly INFO
      log) when `SMTP_HOST`/`ALERT_EMAIL_TO` are unconfigured — the compose
      default — and the `unique=True` cron slot bounds fleet ticks to one
      execution per slot (`core/health_alerts.py`, `core/saq_worker.py`,
      `api/routes/health.py` `evaluate_readiness`, `test_health_alerts.py`)
- [x] Out-of-process Gatus health sentinel in the compose deployment (PR #1260):
      `deploy/watchdog/` ships a non-root Gatus container wired into
      `docker-compose.yml` (on by default, quiet without email creds) that probes
      the app's health surface independently of the system worker the in-band
      cron depends on, closing the full-outage case the FAR-1446 cron cannot
      cover (`deploy/watchdog/config.yaml`, `deploy/watchdog/Dockerfile`,
      `test_watchdog_config.py`, `test_watchdog_container.py`)
- [x] In-process worker-liveness watchdog (ADR 021): a plain asyncio task in the
      web-process FastAPI lifespan — deliberately NOT an SAQ cron, so the alert
      cannot depend on the worker path it watches — reads SAQ worker_info and the
      `fire_due_triggers` cron heartbeats DIRECTLY from Redis every
      `watchdog_tick_seconds` (default 30s). "All workers dead" must hold for
      `watchdog_worker_stale_seconds` (default 180s = 2x the 90s worker_info TTL)
      across BOTH conditions before anything fires; a `_WATCHDOG_BOOT_GRACE_SECONDS`
      (120s) boot grace suppresses alerts AND recoveries on a fresh start. Alerting
      is edge-triggered and multi-machine safe: the incident edge is claimed with
      `SET ... NX` and the recovery edge with `GETDEL` (state survives app restarts
      in Redis with a TTL), so exactly one alert and one recovery email are sent per
      incident, never one per tick. On fire it fans out to EVERY configured channel
      in isolation — generic webhook (Slack-compatible JSON), Microsoft Teams
      MessageCard and email — default-off until at least one channel is set, and
      fails open on Redis read errors (cannot confirm death => never alert)
      (`core/watchdog/worker_liveness.py`, `test_worker_liveness.py`)
- [x] Shared operator-alert context (FAR-1495 / FAR-1499): every operator alert
      identifies the deployment environment (`Environment: <MODULO_ENV>`, empty ->
      `unknown`) and appends the operator's `ALERT_CONTEXT` free text (runbook links,
      escalation notes, ticket pointers — one item per line, blank lines dropped).
      The format is SINGLE-sourced in `core/alert_context.py` (environment line,
      text part, escaped HTML list, and the best-effort stdout stamp) so the
      readiness-degradation cron (`core/health_alerts.py`) and the worker-liveness
      watchdog (`core/watchdog/worker_liveness.py`) can never drift, and the
      webhook/Teams channels carry the SAME suffix as the email text part
      (`_alert_context_suffix`), not just email. The context is bounded
      (`MAX_CONTEXT_LINES` 20, `MAX_CONTEXT_LINE_CHARS` 300, environment line always
      survives) and escaped in HTML (operator free text is untrusted); `ALERT_CONTEXT`
      is `repr=False` so it never enters a log or the stdout stamp
      (`core/alert_context.py`, `test_alert_context.py`, `test_health_alerts.py`,
      `test_worker_liveness.py`)

## Known Gaps

- **No PRD section reference.** The health endpoints are an internal infrastructure
  concern spanning deployment, monitoring, and operations; no single PRD section covers
  liveness/readiness.

## QA History

- 2026-10-06: **Improve Architecture product-map walk** — closed two untracked
  sub-surfaces on this tracker, both invisible to the feature graph and to
  Assistant's `search_documentation` indexer. (1) The in-process worker-liveness
  watchdog (ADR 021 worker-resilience) shipped with no product-map home at all —
  the module carries no `feat-*` reference and this entry cited neither it nor its
  `test_worker_liveness.py` suite; tracked with its sustained-edge alerting, boot
  grace, multi-machine atomic claim and multi-channel fan-out. (2) The FAR-1495 /
  FAR-1499 shared operator-alert context (`core/alert_context.py`) that makes every
  readiness-cron and watchdog alert name the environment and carry `ALERT_CONTEXT`
  across email, webhook and Teams; tracked and cited. Also corrected the stale
  claim in `docs/configuration-reference.md` ("readiness and worker-liveness
  watchdog emails ... do not carry an environment line" — they do since FAR-1495),
  documented the previously-undocumented `ALERT_CONTEXT` setting there and in
  `.env.prod.example`, and added the watchdog module + alert-context module to this
  entry's `code:`/`unit-tests:`. `_ORPHANED_BDD_FEATURES` stays empty.
- 2026-10-05: **Improve Architecture product-map walk** — closed two untracked
  sub-surface gaps on this tracker, both shipped after the last walk and
  invisible to the feature graph / Assistant's `search_documentation` indexer.
  (1) FAR-1446 readiness-degradation email alert: the `health_readiness_alert`
  system cron (every 5 minutes, `unique=True`) emails `ALERT_EMAIL_TO` on a
  hysteresis-confirmed degraded/unavailable transition plus one recovery email,
  evaluating the SAME `evaluate_readiness` implementation the `/healthz/ready`
  route now delegates to, with Redis-backed edge state and quiet-when-
  unconfigured semantics (`core/health_alerts.py`, `core/saq_worker.py`,
  `api/routes/health.py`); cited here with its `test_health_alerts.py` unit
  suite. (2) The compose deployment's out-of-process Gatus sentinel
  (`deploy/watchdog/*`, `docker-compose.yml`), the full-outage leg the in-band
  cron deliberately does not cover; cited with `test_watchdog_config.py` /
  `test_watchdog_container.py`. `_ORPHANED_BDD_FEATURES` stays empty.
- 2026-08-26: **product-map review pass** — closed the "No BDD feature
  files" gap: added `backend/tests/bdd/features/infra/health.feature` + `test_health_steps.py`
  (7 scenarios, self-contained: real health router in a fresh app with only the per-check
  probes patched). Covers liveness advisory, ok/degraded/unavailable aggregation, the 503
  gate, and the FAR-199 dispatcher-reconcile two-tier (unavailable gates, degraded stays
  advisory). Status: covered.
- 2026-08-25: **product-map review pass** — restored this entry as part of
  rebuilding the `docs/product-map/` feature graph that was lost from the public tree.
  Registered the dangling `feat-infra-health` reference: `backend/tests/unit/api/test_health.py`
  documented the per-check-timeout feature with a `feat-infra-health` tag that resolved
  nowhere. Re-verified all 19 behaviours against `backend/src/modulo/api/routes/health.py`,
  `fly.toml`, and `.github/workflows/uptime-monitor.yml`; status: covered.
