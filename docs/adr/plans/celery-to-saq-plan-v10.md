# Celery → SAQ Migration Plan — v10

> **Status**: LOCKED — v10 (2026-07-31), after 9 review iterations of the 7-lens `plan-review-iterate` skill.
> Version-controlled copy of the plan referenced by [ADR 017](../017-celery-to-saq-migration.md) and by the
> delivery-plan tasks `task-saq-migration-pr-a`, `task-saq-migration-pr-b`, `task-saq-migration-pr-c`.

---

## 1. Goal

Replace Celery 5.x (Redis broker, prefork pool) with SAQ 0.26.4 (asyncio-native task queue) as Modulo's task
substrate. Modulo's run workload is I/O-bound async waiting (E2B sandbox dispatch); Celery's process-per-worker
prefork model blocks an OS thread per async wait that an asyncio-native worker would simply await. The recurring
production failures are a structural mismatch, not configuration bugs.

## 2. Substrate selection

**SAQ 0.26.4** selected after comparing:

- **Arq** — async-native and similar in spirit, but effectively maintenance-only; no active development cadence to
  rely on for a production substrate.
- **Hand-rolled asyncio worker** — full control but re-implements a retry/sweeper/cron/dedupe stack; reintroduces
  bespoke distributed-system code under a different name.
- **SAQ** — asyncio-native, actively maintained, ships verified retry semantics, sweeper recovery, cron jobs with
  `unique` overlap control, deterministic job keying, and Lua-based dedupe. Imposed trade-offs (single queue per
  worker, singular concurrency value per worker, job knobs set at enqueue) verified against source and designed
  around.

SAQ 0.26.4 semantics verified against source during review: retry off-by-one, sweeper behaviour, cron safety,
web bind, CLI invocation. Pinned in `pyproject.toml`.

## 3. Worker topology

- **TWO worker processes per machine**, both asyncio-native:
  - **runs worker**: queue=`runs`, concurrency=2, no web UI.
  - **system worker**: queue=`system`, concurrency=2, all cron jobs, web UI on port 8081 bound to `127.0.0.1`
    (fly-ssh only) via a custom runner (`aiohttp` has no host flag; the plain CLI binds `0.0.0.0`). AUTH required
    and **fail-closed**: entrypoint refuses to boot if `SAQ_AUTH_PASSWORD` or `SAQ_AUTH_USERNAME` are unset.
- `runs_manual` + `runs_automated` collapse into **ONE `runs` queue** (the entire Celery worker process is
  deleted, so the collapse loses nothing).
- Global run concurrency: 2/machine × 2–5 machines = 4–10 concurrent runs.

## 4. Database as system of record

- DB remains the system of record for run state. Three columns added: `dispatcher`, `saq_job_id`, `claim_token`.
  `re_enqueue_count` cut (re-enqueue detection moved to log-line ingestion).
- `dispatcher_reconcile` (system cron, every 60s) recovers "DB says job should exist, Redis says none": verifies via
  `queue.job(run:{id})`, repairs partial eviction with queue-name-derived key deletion, and re-enqueues — gated on
  the enqueue return value so it never loops.
- Capacity gating is **DB-deferred**: capacity-blocked runs are never enqueued, stay `pending`, and
  `dispatcher_reconcile` re-dispatches them when capacity frees.

## 5. Delivery sequencing — three PRs

- **PR A**: foundation + spike (hard gate) + tests-first. SPIKE runs a throwaway worker against an
  identically-configured dev Upstash instance (never production) and settles the remaining empirical unknowns
  (Section 8). Raw spike evidence is committed with PR A.
- **PR B**: routing (`dispatch_run` single gating point) + the 3 columns + `saq` error enum + shadow mode +
  staging smoke. SAQ runs in shadow on production (`SAQ_ENABLED=false`): `execute_run` keeps routing to Celery
  (`dispatcher=NULL`), `resume_run` routes to SAQ (`dispatcher='saq'`). The staging smoke flips `SAQ_ENABLED=true`
  on dedicated Upstash + Postgres with queue prefixes `staging-runs`/`staging-system` so the acceptance
  (`dispatcher='saq'` + `claim_count==1`) is reachable.
- **PR C**: cutover + Celery removal, with a **48–72h hold**. Sequenced rollout: deploy the image with BOTH
  Celery+SAQ and `SAQ_ENABLED=true` on all machines first, verify SAQ green, then deploy the Celery-removal
  image — never a scheduler-less window.

## 6. At-most-once residual and mitigations

Accepted residual: an event-loop stall ≥450s freezes the DB heartbeat and `job.update()`, the sweeper re-queues,
and a retry can double-execute. Mitigations:

- **Claim-token fence**: `claim_token` is a DISTINCT per-claim value (SAQ retries reuse the same `saq_job_id`),
  verified on heartbeat (cheap early-abort), immediately before the E2B dispatch (the load-bearing site), and before
  other side-effect commits. The finally-stale write (`heartbeat_at = now() - 8min`) is itself claim-token-fenced so
  a superseded original cannot stale a successor's fresh heartbeat.
- **E2B idempotency key** (per-claim, default ON): key `run:{id}:e2b:{claim_token}` set via SETNX before dispatch
  (atomic — exactly one executor wins), fenced DEL on failure, retained until terminal + upper TTL bound (~8h).
  Also catches E2B client-level transient retries within one claim.

## 7. Multi-machine cron safety

Cron jobs run on every machine's system worker. Safety comes from advancing `next_fire_at` **atomically at
enqueue time**: a conditional `UPDATE ... WHERE next_fire_at <= now() RETURNING id` picks the due rows, and only
the returned rows are enqueued. A second machine's tick sees `next_fire_at` already advanced and skips.
Lost-epoch-on-crash (process dies between the atomic UPDATE and the enqueue) is accepted and self-heals on the
next tick, with alerting as the closure.

## 8. Empirical unknowns to settle in the SPIKE (PR A hard gate)

- Upstash `maxmemory-policy` (volatile-lru vs allkeys-lru decides whether the full partial-eviction machinery is
  load-bearing).
- ttl semantics (enqueue- vs start-origin decides `execute_run` ttl of ≥8h vs 300s).
- Retry timing determinism (`retry_backoff=False`).
- E2B transient-retry distribution (sets `SAQ_RUN_RETRIES` at ~p99.5).

## 9. CI gating

- SAQ integration tests fold into `ci.yml` as jobs, keeping the workflow name "CI: Fast Validation" —
  `merge-queue.yml` gates on that workflow name because branch protection is unavailable on this repo
  (HTTP 403, free tier).
- Jobs run on hosted parallel `ubicloud-standard-2` runners; CI wall-clock is `max(jobs)` (~40 min).
- A one-retry wrapper (pytest-rerunfailures) bounds flake churn; a red-after-rerun SAQ job is expected to trigger
  Branch Fixer.

## 10. Rollback (post-PR-C, ordered)

Halt new dispatches → drain/fail SAQ jobs (never re-dispatch running rows onto old Celery unguarded) →
SQL-flip `dispatcher=NULL` + `dispatched_at=NULL` as the owner role (RLS-safe, count-asserted) → run the 3 column
down-revisions → deploy the pre-cutover image SHA. Recover from DB state, not queue state. 48–72h green before
anything is considered unreversible.

## 11. Schema / consequences

- 3 columns (`dispatcher`, `saq_job_id`, `claim_token`) + a permanent `'saq'` value added to the error enum (with
  constraint UPDATE and validator). Two migrations in PR B; **no PR C migration**. The enum value is permanent
  because PostgreSQL cannot drop an enum value still referenced by rows; rows would be reclassified to `'internal'`
  if it were ever removed.
- The stale AGENTS.md claim ("13 workflows, self-hosted runner, concurrency group") is corrected in the same PR set.
