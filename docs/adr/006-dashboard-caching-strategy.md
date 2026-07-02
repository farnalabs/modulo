# ADR 006 — Dashboard Performance: Application Cache Over Materialized View

**Date**: 2026-07-01  
**Status**: Active

---

## Context

The org dashboard (`GET /api/v1/dashboard/summary`) runs ~9 aggregate SQL queries per page load — status counts, eval pass rates, team breakdowns, 7-day trends. On a cold load this takes ~50–200ms depending on data volume.

Two options were considered to make it consistently fast:

1. **Redis + in-memory application cache** (60s TTL)
2. **Materialized database view** (`mv_org_dashboard`) refreshed periodically

## Decision

Use application-level caching (Redis with in-memory fallback). Do not add a materialized view.

Concretely:

- The `/summary` endpoint checks a Redis key (`dashboard:summary:{org_id}`) first; if present, returns it in ~5ms with zero DB queries.
- If Redis is unavailable (no `REDIS_URL`, connection failure), falls back to an in-memory dict with the same 60s TTL.
- The cache is written after every successful response, so it warms after the first cold load.
- No database schema changes, no migration, no `REFRESH` scheduler.

## Why Not a Materialized View

1. **Redis is already a dependency.** It serves as the Celery broker, rate limiter store, event bus, and WS-token cache. Every deployment (including self-hosted) already has it via `docker-compose.yml`. Adding a materialized view would add a *second* cache layer on top of the one we already run.

2. **No pg_cron / pg_timetable requirement.** A materialized view needs a scheduler to `REFRESH MATERIALIZED VIEW` on a cadence. That's either pg_cron (requires `shared_preload_libraries`) or a Celery task — both more operational surface area than a `setex` call.

3. **Self-hosted simplicity.** Customers who deploy without Redis (the no-Redis deployment path) fall through to the in-memory cache, which is per-process and works identically for single-worker setups — the most common self-hosted topology. A materialized view would still require Postgres and the refresh scheduler, which is strictly *more* requirements, not fewer.

4. **Data volume doesn't justify it.** At zero-to-low user counts, the 9 aggregate queries finish in under 100ms. The application cache eliminates repeat load cost entirely. The materialized view's benefit only materializes when the DB is under enough write load that 9 aggregate queries per request saturate it — a scale we are not at and may never reach for self-hosted instances.

5. **Staleness semantics are equivalent.** A 60s application cache and a 60s refresh on a materialized view have identical staleness windows. The application cache is *easier to invalidate* (drop the Redis key) when a relevant write occurs — something a materialized view can't do without a trigger or LISTEN/NOTIFY.

## What This Means for Code

| Concern | Approach |
|---|---|
| Cache backend | Redis via `redis.asyncio.Redis.from_url()` |
| Cache fallback | In-memory `dict[str, tuple[float, data]]` per process |
| Cache key | `dashboard:summary:{org_id}` |
| TTL | 60 seconds (configurable via `_DASHBOARD_CACHE_TTL`) |
| Write-through | Cache set after every successful `/summary` response |
| No-Redis path | Transparent fallback — no config change needed |
| Query merging | Also applied (merged redundant eval queries) — orthogonal to caching |

## When to Revisit

- A customer reports the dashboard is slow with their real data *and* the 60s cache is insufficient
- The app scales to multiple workers without Redis (in-memory cache becomes per-worker, so each worker gets a cold cache)
- Write volume reaches a point where the dashboard's aggregate queries contend with production writes

At that point, a materialized view is a straightforward migration — the SQL is well-understood and the refresh trigger can piggyback on the existing Celery schedule.

## Related Documents

- PRD §14 — org dashboard feature spec
- `backend/src/modulo/api/routes/dashboard.py` — implementation
- ADR 002 — Database Abstraction Strategy
