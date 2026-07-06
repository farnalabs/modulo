---
id: feat-core-pkg0-celery-optional
prd: 14
delivery-tasks: [task-pkg0-celery-optional]
bdd:
  - backend/tests/bdd/features/pipelines/scheduling.feature
code:
  - backend/src/modulo/celery_app.py
  - backend/src/modulo/core/cron_scheduler.py
  - backend/src/modulo/core/in_process_scheduler.py
  - backend/src/modulo/core/trigger_engine/polling.py
  - backend/src/modulo/core/rate_limiter.py
  - backend/src/modulo/core/reports/scheduler.py
  - backend/src/modulo/core/cleanup_jobs/webhook_dedup_cleanup.py
  - backend/src/modulo/core/notifier/celery_tasks.py
  - backend/src/modulo/api/main.py
  - backend/pyproject.toml
unit-tests:
  - backend/tests/unit/celery_app/test_celery_import_guard.py
  - backend/tests/unit/in_process_scheduler/test_in_process_scheduler.py
depends-on: []
status: partial
---

# Celery/Redis optional — in-process asyncio fallback for scheduling and rate limiting

Make Celery and Redis optional dependencies so Modulo runs without a Redis process in standalone/development mode, falling back to in-process asyncio schedulers for cron and polling triggers. ADR 003 pre-requisite for PyPI packaging.

## Behaviours

### Dependency packaging

- [x] `celery[redis]` and `redis` moved to `[redis]` extras in pyproject.toml
- [x] Core install (`pip install modulo`) does not require Celery or Redis
- [x] `pip install modulo[redis]` installs Celery + Redis (extras group `redis = [celery[redis], redis]` exists in pyproject.toml at `[project.optional-dependencies]`; `redis` Python client is also in main deps for rate-limiting/event-broker fallback — `modulo[redis]` additionally pulls in `celery[redis]`)

### Celery app laziness

- [x] `celery_app.py` lazy-initialises `Celery()` only when Redis is configured
- [x] `celery_app` module-level attribute is `None` when Redis/Celery unavailable
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

### In-process schedulers

- [x] `in_process_scheduler.py` provides asyncio-based cron scheduler loop
- [x] `in_process_scheduler.py` provides asyncio-based polling scheduler loop
- [x] Both loops poll database every 30s for due triggers
- [x] Each due trigger fired as a separate `asyncio.create_task()`
- [x] Scheduler tasks cancelled cleanly on application shutdown

### Application wiring

- [x] `main.py` lifespan starts in-process schedulers when `redis_url` is empty/unconfigured
- [x] `main.py` lifespan skips in-process schedulers when Redis is configured
- [x] Startup log message indicates which scheduling mode is active
- [x] Warning logged for multi-replica deployments without Redis

### Rate limiting (already in main)

- [x] `RateLimiterRegistry` falls back to in-memory `TokenBucket` when Redis unavailable
- [x] Startup warning for in-memory rate limiting mode
- [x] SQLite mode disables rate limiting entirely

### Graceful degradation

- [x] App starts without Redis installed or configured
- [x] App starts without Celery package installed (guarded imports on all 5 Celery-dependent modules)
- [x] All non-scheduling API endpoints work without Celery/Redis
- [x] Scheduled triggers silently skipped when no scheduler active
- [x] Rate limiting works (in-memory) without Redis

### Edge cases

- [ ] Redis configured but unreachable at startup — falls back with warning
- [ ] Redis becomes unreachable mid-session — degraded state (triggers stop firing)
- [ ] Redis becomes available after starting without it — requires restart
- [x] `redis_url` set to empty string treated same as unset — `main.py:413` uses `if not settings.redis_url:` which treats empty string `""` as falsy, routing to in-process scheduler path

### Error Handling

- [x] `ImportError` for missing Celery package caught gracefully at 5 guarded-import sites (celery_app.py, cron_scheduler.py, polling.py, celery_tasks.py, webhook_dedup_cleanup.py, reports/scheduler.py)
- [x] In-process scheduler loops catch `asyncio.CancelledError` explicitly and break cleanly
- [x] Each in-process scheduler loop catches generic `Exception`, logs via `_log.exception()`, and continues the loop — a single DB query failure does not kill the scheduler
- [x] Each fire wrapper (`_fire_cron_wrapper`, `_fire_polling_wrapper`) wraps its fire call in `try/except Exception` — a single trigger failure does not prevent other triggers from firing
- [x] Celery `CronFireTask` uses `autoretry_for = (Exception,)` with 3 retries at 60s intervals
- [x] `_sync_with_db()` in `DatabaseCronScheduler` catches `Exception` and returns empty list on failure — a DB tick failure does not crash the beat scheduler
- [x] `Log_event` errors are localised — a failed TriggerEvent insert does not roll back the run creation (session.flush() is the last op)
- [ ] No health check or liveness endpoint for in-process scheduler loops — a stuck loop is not detectable externally
- [ ] `asyncio.run()` in sync Celery beat methods calls (`_sync_with_db` at cron_scheduler.py:356) has no guard for an already-running event loop
- [ ] No circuit breaker or exponential backoff in scheduler loops — on persistent DB failure, loops busy-poll every 30s with full stack trace logging

### Resilience

- [x] In-process schedulers run as independent `asyncio.Task`s — cancellation on shutdown is clean (CancelledError caught, loop exits)
- [x] Scheduler engine is disposed on shutdown via `dispose_scheduler_engine()`
- [x] Celery beat scheduler and in-process scheduler are mutually exclusive — main.py lifespan starts one or the other based on `redis_url`, never both
- [x] `fire_cron_trigger` uses `FOR UPDATE` row lock to serialise concurrent fires across Celery worker replicas
- [x] In-process polling triggers gracefully skip rows missing `connector_instance_id` (logged as warning)
- [x] In-process cron triggers tolerate missing/invalid `snapshot_id` in config_json by falling back to `uuid.uuid4()`
- [ ] No health-check endpoint to verify scheduler is running — if a loop silently exits (bug in exception handler), triggers stop firing with no alert
- [ ] `_scheduler_engine` in `cron_scheduler.py` is module-level and never disposed — connection pool leak on Celery beat restart

## Known Gaps

- No integration test verifying app starts and fires triggers without Redis (requires testcontainer)
- No runtime multi-replica concurrency warning guard
- Connection errors to Redis at startup not caught gracefully — `Celery()` instantiation does NOT try to connect to the broker; the app starts successfully even with an unreachable Redis URL, and errors surface only at task-send time with no startup warning
- Redis mid-session failure not handled (triggers stop firing, no reconnection)
- `modulo[redis]` extras group defined but not tested end-to-end
- `redis_url` empty-string vs unset edge case not tested
- `_scheduler_engine` (in `cron_scheduler.py`) created via `_get_engine()` is never explicitly disposed — the connection pool lives for the Celery beat process lifetime
- `asyncio.run()` in `_sync_with_db()` (cron_scheduler.py:356) raises `RuntimeError` if called from within an already-running event loop — no guard present

## QA History

- 2026-07-01 (improve-architecture index 38): Added guarded imports to celery_app.py, cron_scheduler.py, celery_tasks.py, webhook_dedup_cleanup.py, reports/scheduler.py. Moved celery+redis to [redis] extras. Created 11 import-guard unit tests and 14 in-process scheduler unit tests. Replaced 2 @awaiting-implementation BDD scenarios with real ones + step definitions. Removed stale "branch not merged" gap (code already on main).
- 2026-07-06 (cross-cutting QA): Verified all behaviours against code — marked `modulo[redis]` extras and `redis_url` empty-string edge case as checked [x]. Added Error Handling section (10 items: 7 [x] + 3 [ ]) and Resilience section (8 items: 6 [x] + 2 [ ]). Updated Known Gaps with accurate connection-error description (Celery() does not raise on bad URL) and 2 new gaps (engine disposal leak, asyncio.run guard missing). Created website docs stub at `Website/modulo-website/src/docs/scheduling/in-process-scheduler.md`.
