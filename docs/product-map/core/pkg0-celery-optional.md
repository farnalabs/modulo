---
id: feat-core-pkg0-celery-optional
prd: 8.5
delivery-tasks: [task-pkg0-celery-optional]
bdd:
  - backend/tests/bdd/features/pipelines/scheduling.feature
code:
  - backend/src/modulo/core/saq_worker.py
  - backend/src/modulo/core/cron_helpers.py
  - backend/src/modulo/core/dispatch.py
  - backend/src/modulo/core/pipeline_execution.py
  - backend/src/modulo/core/trigger_engine/polling.py
  - backend/src/modulo/api/main.py
  - backend/pyproject.toml
unit-tests:
  - backend/tests/unit/test_saq_worker.py
  - backend/tests/unit/test_dispatch.py
  - backend/tests/unit/cron_helpers/test_dispatcher_reconcile.py
depends-on: [feat-core-trigger-system, feat-core-pipeline-execution, feat-core-db-abstraction-core]
status: partial
---

# Celery removed — SAQ cutover (PR C)

Celery was fully removed in PR C of the Celery->SAQ migration. Modulo now runs
on **SAQ 0.26.4**: two worker processes (`runs` + `system`), Redis as the
broker, and DB as the system of record. `SAQ_ENABLED` is gone — SAQ is the only
dispatch path.

- `runs` worker — executes `execute_run` / `resume_run` jobs and per-item fire jobs.
- `system` worker — owns the scheduler (`fire_due_triggers`) + `dispatcher_reconcile` + system crons; web UI on 8081 bound to 127.0.0.1 (fail-closed auth).

Redis is hard-required at startup (`REDIS_URL`).

## Behaviours

### Dispatch (SAQ single path)

- [x] `dispatch_run` enqueues `execute_run` / `resume_run` to the SAQ runs queue with per-job knobs
- [x] `dispatcher` column always reads `'saq'` (no Celery routing branch)
- [x] Capacity gating: capacity-blocked runs return `'deferred'` with no enqueue / no `dispatched_at`; `dispatcher_reconcile` re-dispatches when capacity frees
- [x] `dispatched_at` written BEFORE enqueue (single gating point, F3e)
- [x] Enqueue failure: fail-fast in webhook handlers (202), backoff elsewhere; final failure marks `dispatch_failed` + expires webhook dedup

### Scheduler (system worker)

- [x] `fire_due_triggers` cron advances `next_fire_at` ATOMICALLY at enqueue time (multi-machine safe, F1)
- [x] Per-item fire jobs (`fire_cron_trigger` / `fire_polling_trigger` / `fire_report_trigger`) on the runs queue
- [x] `dispatcher_reconcile` cron re-dispatches runs whose SAQ job is missing (partial-eviction repair, prefix-aware)
- [x] claim-expiry, retention, webhook-dedup cleanup, stale_run_recovery as system crons
- [x] Exactly one scheduler: no Celery beat, no in-process scheduler

### Execution (shared core)

- [x] claim / execute / heartbeat / complete / resume shared in `core/pipeline_execution.py`
- [x] Atomic claim UPDATE (no check-then-act window), claim cap
- [x] `_mark_complete` writes `'complete'` (DB enum)
- [x] stale_run_recovery scoped: never_dispatched / worker_lost branches match `dispatcher IS NULL OR dispatcher != 'saq'` only; capacity_timeout backstop unscoped

### Error tracking

- [x] `saq_hooks.after_process` ingests failed jobs (source `'saq'`)
- [x] `'celery'` kept as a legacy error-source enum value (no migration)

### Health / deploy gates

- [x] `/healthz/ready` machine-scoped SAQ worker gate — 503 after 4 consecutive stale probes (SAQ_HARD_GATE, default true)
- [x] Entrypoint runs only the 2 SAQ workers (Celery removed), fail-closed auth, crash-loop guard
- [x] fly.toml health check path `/healthz/ready`, interval <30s, kill_timeout >= 120s, restart policy
- [x] Deploy hold gate (deploy.yml `hold-check` / `SAQ_HOLD`) **retired 2026-08-05 (PR #752)** — cutover complete; deploys are now gated only by the deploy throttle (`DEPLOY_INTERVAL_HOURS`) + the app-side `SAQ_HARD_GATE` readiness gate (a separate mechanism that stays)

### Local dev

- [x] `docker-compose.yml` + `docker-compose.local.yml` ship `saq-runner` + `saq-system` services
- [x] `uv run python -m saq modulo.core.saq_worker.runs_settings` / `uv run python -m modulo.core.saq_worker` documented
- [x] Local Redis required (`REDIS_URL`)

## Known Gaps

- `saq-system` service required for local cron/triggers — a dev running only postgres+redis+uvicorn silently gets zero trigger firing (documented)
- Running both the compose `saq-system` AND a manual `uv run` system worker double-starts crons (safe via atomic `next_fire_at` + `unique=True`, but stated in docs)
- `'celery'` remains a valid error-source value for historical rows; no down-revision needed (PostgreSQL cannot drop enum values still referenced)

## QA History

- 2026-07-01 (improve-architecture index 38): Guarded imports + `[redis]` extras era (Celery optional).
- 2026-07-06 (cross-cutting QA): Verified guarded-import behaviours.
- 2026-08-02 (PR C cutover): Celery fully removed. SAQ is the only dispatch path; `SAQ_ENABLED` deleted; dispatch_run flattened to unconditional 'saq'; `MODULO_CELERY_DB_POOL_*` settings removed; entrypoint runs only SAQ workers; deploy hold-check (`SAQ_HOLD`) added; local-dev worker services shipped. (The `SAQ_HOLD` hold gate was later retired 2026-08-05 in PR #752.)
