---
title: In-Process Scheduler (No-Celery Mode)
---

# In-Process Scheduler (No-Celery Mode)

Modulo can run without Celery or Redis for standalone/development environments. When `REDIS_URL` is empty or unset, the app uses in-process asyncio-based schedulers for cron and polling triggers instead of Celery beat.

- Mode selection: automatic — `REDIS_URL` empty → in-process; `REDIS_URL` set → Celery beat
- Cron scheduler loop: polls DB every 30s for due triggers, fires via `fire_cron_trigger()`
- Polling scheduler loop: polls DB every 30s for due polling triggers, fires via `fire_polling_trigger()`
- Cleanup loop: deduplication cleanup via `cleanup_scheduler_loop()`
- All three loops run as independent `asyncio.Task`s, cancelled cleanly on shutdown
- Rate limiting falls back to in-memory `TokenBucket` when Redis is unavailable
- PRD: §14 (Future Roadmap — Cron Trigger)

**Note:** In-process schedulers do not coordinate across replicas. For multi-replica deployments, configure `REDIS_URL` to use Celery beat.

See the [PRD §14](https://github.com/farnalabs/modulo/blob/main/docs/prd.md#14-future-roadmap) for the full specification.
