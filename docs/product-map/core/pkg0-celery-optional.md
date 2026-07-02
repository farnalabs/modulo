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
- [ ] `pip install modulo[redis]` installs Celery + Redis (extras section exists but `modulo[redis]` not tested)

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
- [ ] `redis_url` set to empty string treated same as unset (checked in main.py — needs verification)

## Known Gaps

- No integration test verifying app starts and fires triggers without Redis (requires testcontainer)
- No runtime multi-replica concurrency warning guard
- Connection errors to Redis at startup not caught gracefully (Celery() instantiation raises)
- Redis mid-session failure not handled (triggers stop firing, no reconnection)
- `modulo[redis]` extras group defined but not tested end-to-end
- `redis_url` empty-string vs unset edge case not tested

## QA History

- 2026-07-01 (improve-architecture index 38): Added guarded imports to celery_app.py, cron_scheduler.py, celery_tasks.py, webhook_dedup_cleanup.py, reports/scheduler.py. Moved celery+redis to [redis] extras. Created 11 import-guard unit tests and 14 in-process scheduler unit tests. Replaced 2 @awaiting-implementation BDD scenarios with real ones + step definitions. Removed stale "branch not merged" gap (code already on main).
