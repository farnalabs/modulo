---
id: feat-core-pkg0-celery-optional
prd: 8.5
delivery-tasks: [task-pkg0-celery-optional]
bdd:
  - backend/tests/bdd/features/pipelines/scheduling.feature
code:
  - backend/src/modulo/celery_app.py
  - backend/src/modulo/core/cron_scheduler.py
  - backend/src/modulo/core/trigger_engine/polling.py
  - backend/src/modulo/core/rate_limiter.py
  - backend/src/modulo/core/reports/scheduler.py
  - backend/src/modulo/core/cleanup_jobs/webhook_dedup_cleanup.py
  - backend/src/modulo/core/notifier/celery_tasks.py
  - backend/src/modulo/api/main.py
  - backend/pyproject.toml
unit-tests:
  - backend/tests/unit/celery_app/test_celery_import_guard.py
depends-on: [feat-core-trigger-system, feat-core-pipeline-execution, feat-core-db-abstraction-core]
status: partial
---

# Redis hard-required — in-process scheduler removed

Redis is now hard-required at startup. The in-process asyncio scheduler fallback (`InProcessScheduler`) has been removed — if `REDIS_URL` is not set, the app raises `RuntimeError` and refuses to start.

This removes the risk of duplicate trigger fires in multi-replica deployments and eliminates a separate connection pool and asyncio task management layer that added complexity without benefit.

## Behaviours

### Dependency packaging

- [x] `celery[redis]` and `redis` moved to `[redis]` extras in pyproject.toml
- [x] `pip install modulo[redis]` installs Celery + Redis (extras group `redis = [celery[redis], redis]` exists in pyproject.toml at `[project.optional-dependencies]`; `redis` Python client is also in main deps for rate-limiting/event-broker fallback — `modulo[redis]` additionally pulls in `celery[redis]`)

### Celery app laziness

- [x] `celery_app.py` lazy-initialises `Celery()` only when Redis is configured
- [x] `celery_app` module-level attribute is `None` when Celery unavailable
- [x] ImportError for missing Celery package caught gracefully (startup warning)
- [ ] Connection errors to Redis caught gracefully (startup warning)

### Guarded imports

- [x] `cron_scheduler.py` guards `from celery import ...` with `try/except ImportError`
- [x] `polling.py` guards `from celery import ...` with `try/except ImportError`
- [x] `celery_tasks.py` guards `from celery import ...` with `try/except ImportError`
- [x] `webhook_dedup_cleanup.py` guards `from celery import ...` with `try/except ImportError`
- [x] `reports/scheduler.py` guards `from celery import ...` with `try/except ImportError`
- [x] Celery-dependent classes only defined when Celery is installed
- [x] Non-Celery fire logic extracted to `fire_cron_trigger()` / `fire_polling_trigger()` shared async functions

### Application wiring

- [x] `main.py` lifespan errors at startup if `REDIS_URL` is not set
- [x] In-process scheduler (`in_process_scheduler.py`) deleted entirely
- [x] All imports and references to `in_process_scheduler` removed from main.py
- [x] Test file for in-process scheduler deleted

### Rate limiting (already in main)

- [x] `RateLimiterRegistry` falls back to in-memory `TokenBucket` when Redis unavailable
- [x] Startup warning for in-memory rate limiting mode
- [x] SQLite mode disables rate limiting entirely

### Edge cases

- [x] App refuses to start when `REDIS_URL` is unset — raises `RuntimeError` with clear message
- [x] `redis_url` set to empty string treated same as unset — same `RuntimeError` raised

### Error Handling

- [x] `ImportError` for missing Celery package caught gracefully at guarded-import sites (celery_app.py, cron_scheduler.py, polling.py, celery_tasks.py, webhook_dedup_cleanup.py, reports/scheduler.py)
- [x] Celery `CronFireTask` uses `autoretry_for = (Exception,)` with 3 retries at 60s intervals
- [x] `_sync_with_db()` in `DatabaseCronScheduler` catches `Exception` and returns empty list on failure — a DB tick failure does not crash the beat scheduler
- [x] `Log_event` errors are localised — a failed TriggerEvent insert does not roll back the run creation (session.flush() is the last op)
- [x] `CronFireTask.run()` now handles async Celery pool via try/except `RuntimeError` — same pattern as `PollingFireTask` (fixed in improve-architecture index 236)
- [ ] `asyncio.run()` in `_sync_with_db()` (cron_scheduler.py:356, polling.py:526, reports/scheduler.py:415) has no guard for an already-running event loop — Celery beat only runs in sync context, low risk
- [x] `cron_scheduler.py:_set_rls_org()` now uses dialect check with `session.info` fallback for non-Postgres backends (fixed in improve-architecture index 236)

### Resilience

- [x] `fire_cron_trigger` uses `FOR UPDATE` row lock to serialise concurrent fires across Celery worker replicas
- [ ] No health-check endpoint to verify scheduler is running — if Celery beat silently exits, triggers stop firing with no alert
- [ ] `_scheduler_engine` in `cron_scheduler.py` and `_engine` in `polling.py` are module-level and never disposed — connection pool leak on Celery beat restart

## Known Gaps

- Connection errors to Redis at startup not caught gracefully — `Celery()` instantiation does NOT try to connect to the broker; the app starts successfully even with an unreachable Redis URL, and errors surface only at task-send time with no startup warning
- Redis mid-session failure not handled (triggers stop firing, no reconnection)
- `_scheduler_engine` (in `cron_scheduler.py`) AND `_engine` (in `polling.py`) are created via module-level `_get_engine()` and never explicitly disposed — connection pools live for the Celery beat process lifetime
- `asyncio.run()` in `_sync_with_db()` (cron_scheduler.py:356, polling.py:526, reports/scheduler.py:415) raises `RuntimeError` if called from within an already-running event loop — no guard present (Celery beat only runs in sync context, so this is low-risk)
- [RESOLVED in improve-architecture index 236] `CronFireTask.run()` now handles async Celery pool via `try/except RuntimeError` — matching the PollingFireTask pattern. Gap is resolved.
- [RESOLVED in improve-architecture index 236] `cron_scheduler._set_rls_org()` now checks dialect and falls back to `session.info["organisation_id"]` on non-Postgres. Gap is resolved.
- `_ENGINE` in `reports/scheduler.py:57` is module-level, created via `_get_engine()`, and never explicitly disposed — same engine leak pattern as cron_scheduler.py and polling.py (undocumented additional gap)
## QA History

- 2026-07-01 (improve-architecture index 38): Added guarded imports to celery_app.py, cron_scheduler.py, celery_tasks.py, webhook_dedup_cleanup.py, reports/scheduler.py. Moved celery+redis to [redis] extras. Created 11 import-guard unit tests and 14 in-process scheduler unit tests. Replaced 2 @awaiting-implementation BDD scenarios with real ones + step definitions. Removed stale "branch not merged" gap (code already on main).
- 2026-07-06 (cross-cutting QA): Verified all behaviours against code — marked `modulo[redis]` extras and `redis_url` empty-string edge case as checked [x]. Added Error Handling section (10 items: 7 [x] + 3 [ ]) and Resilience section (8 items: 6 [x] + 2 [ ]). Updated Known Gaps with accurate connection-error description (Celery() does not raise on bad URL) and 2 new gaps (engine disposal leak, asyncio.run guard missing). Created website docs stub at `Website/modulo-website/src/docs/scheduling/in-process-scheduler.md`.
- 2026-07-06 (improve-architecture index 236): Fixed CRITICAL — `CronFireTask.run()` now handles async Celery pool (was bare `asyncio.run()` without existing-loop guard). Fixed CRITICAL — `cron_scheduler._set_rls_org()` now checks dialect and falls back to `session.info` on non-Postgres (was PG-only `set_config()` that would crash on SQLite/MariaDB — unlike `polling.py` which already had the correct pattern). Updated Known Gaps: corrected stale `redis_url` empty-string gap (IS tested), merged cron + polling engine leak into single gap, added 2 new gaps (CronFireTask async pool gap now fixed, cron_scheduler PG-only RLS now fixed). Added 2 new unchecked items (asyncio.run guard in 3 _sync_with_db callers, same engine leak affects polling.py).
- 2026-07-27 (rm-scheduler): Removed in-process scheduler (`in_process_scheduler.py` + test file). Redis is now hard-required — app refuses to start without `REDIS_URL`. All references removed from main.py. Updated product-map entry to reflect removal.
