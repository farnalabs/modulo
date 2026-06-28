---
id: feat-core-pkg0-celery-optional
prd: 14.3
delivery-tasks: [task-pkg0-celery-optional]
bdd: backend/tests/bdd/features/pipelines/scheduling.feature
code:
  - backend/src/modulo/celery_app.py
  - backend/src/modulo/core/cron_scheduler.py
  - backend/src/modulo/core/in_process_scheduler.py
  - backend/src/modulo/core/trigger_engine/polling.py
  - backend/src/modulo/core/rate_limiter.py
  - backend/src/modulo/api/main.py
  - backend/pyproject.toml

status: partial
---
# Celery/Redis optional — in-process asyncio fallback for scheduling and rate limiting Make Celery and Redis optional dependencies so Modulo runs without a
Redis process in standalone/development mode, falling back to in-process
asyncio schedulers for cron and polling triggers. ADR 003 pre-requisite for PyPI packaging. ## Behaviours ### Dependency packaging - [ ] `celery[redis]` and `redis` moved to `[redis]` extras in pyproject.toml
- [ ] Core install (`pip install modulo`) does not require Celery or Redis
- [ ] `pip install modulo[redis]` installs Celery + Redis ### Celery app laziness - [ ] `celery_app.py` lazy-initialises `Celery()` only when Redis is configured
- [ ] `celery_app` module-level attribute is `None` when Redis/Celery unavailable
- [ ] ImportError for missing Celery package caught gracefully (startup warning)
- [ ] Connection errors to Redis caught gracefully (startup warning) ### Guarded imports - [ ] `cron_scheduler.py` guards `from celery import ...` with `try/except ImportError`
- [ ] `polling.py` guards `from celery import ...` with `try/except ImportError`
- [ ] Celery-dependent classes only defined when Celery is installed
- [ ] Non-Celery fire logic extracted to `fire_cron_trigger()` / `fire_polling_trigger()` shared async functions ### In-process schedulers - [ ] `in_process_scheduler.py` provides asyncio-based cron scheduler loop
- [ ] `in_process_scheduler.py` provides asyncio-based polling scheduler loop
- [ ] Both loops poll database every 30s for due triggers
- [ ] Each due trigger fired as a separate `asyncio.create_task()`
- [ ] Scheduler tasks cancelled cleanly on application shutdown ### Application wiring - [ ] `main.py` lifespan starts in-process schedulers when `redis_url` is empty/unconfigured
- [ ] `main.py` lifespan skips in-process schedulers when Redis is configured
- [ ] Startup log message indicates which scheduling mode is active
- [ ] Warning logged for multi-replica deployments without Redis ### Rate limiting (already in main) - [x] `RateLimiterRegistry` falls back to in-memory `TokenBucket` when Redis unavailable
- [x] Startup warning for in-memory rate limiting mode
- [x] SQLite mode disables rate limiting entirely ### Graceful degradation - [ ] App starts without Redis installed or configured
- [ ] App starts without Celery package installed
- [ ] All non-scheduling API endpoints work without Celery/Redis
- [ ] Scheduled triggers silently skipped when no scheduler active
- [ ] Rate limiting works (in-memory) without Redis ### Edge cases - [ ] Redis configured but unreachable at startup — falls back with warning
- [ ] Redis becomes unreachable mid-session — degraded state (triggers stop firing)
- [ ] Redis becomes available after starting without it — requires restart
- [ ] `redis_url` set to empty string treated same as unset ## Known Gaps - **Branch not merged**: `origin/pkg/celery-optional` has the full implementation but was never merged to main. Task `task-pkg0-celery-optional` is marked completed in the delivery plan, but the code never landed.
- No unit tests for `in_process_scheduler.py` or guarded-import fallback paths
- `scheduling.feature` is a placeholder — no BDD scenarios exist
- No integration test verifying app starts and fires triggers without Redis
- No runtime multi-replica concurrency warning guard 