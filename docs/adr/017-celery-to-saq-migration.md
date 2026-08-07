# ADR 017 — Celery to SAQ Migration

> Full migration plan: [`docs/adr/plans/celery-to-saq-plan-v10.md`](plans/celery-to-saq-plan-v10.md) (referenced in the delivery-plan tasks `task-saq-migration-pr-a/b/c`).
>
> **Superseded — tracker retired.** This ADR references task IDs (`task-saq-migration-pr-a/b/c`) from the
> retired delivery-plan.json tracker. The tracker is retired (source of truth: Linear); task IDs here are
> historical references only.
>
> **Cutover hold retired — 2026-08-05.** The PR C "48–72h hold" was implemented as the deploy.yml
> `hold-check` job gated on the GitHub `SAQ_HOLD` repo variable. That gate is now **deleted**: the
> cutover is complete (PRs #725/#726/#734 merged) and deploys are no longer gated by `SAQ_HOLD`.

**Date**: 2026-07-31
**Status**: Accepted

---

## Context

Celery 5.x (Redis broker, prefork pool) causes recurring production failures. Modulo's run workload is I/O-bound async waiting (E2B sandbox dispatch), which Celery's prefork pool handles poorly — each worker process blocks an OS thread on an async wait that an asyncio-native worker would simply await. The recurring failures are not configuration bugs; they are a structural mismatch between a process-per-worker model and an I/O-bound, mostly-waiting workload.

The migration plan went through **9 review iterations** using the 7-lens `plan-review-iterate` skill. Each iteration hardened the design: corrected SAQ 0.26.4 semantics were verified against source (retry off-by-one, sweeper behaviour, cron safety, web bind, CLI invocation), environment facts were verified and corrected stale AGENTS.md claims, and the shadow/cutover sequencing was stress-tested. The plan is locked at **v10**.

**SAQ 0.26.4** was selected after deep research comparing alternatives:

- **Arq** — async-native and similar in spirit, but effectively maintenance-only; no active development cadence to rely on for a production substrate.
- **Hand-rolled asyncio worker** — full control but re-implements a retry/sweeper/cron/dedupe stack that is precisely the hard part; reintroduces bespoke distributed-system code under a different name.
- **SAQ** — asyncio-native, actively maintained, and ships the primitives the migration needs: verified retry semantics, sweeper recovery, cron jobs with `unique` overlap control, deterministic job keying, and Lua-based dedupe. The trade-offs it does impose (single queue per worker, singular concurrency value per worker, job knobs set at enqueue) were verified against source and designed around.

---

## Decision

Replace Celery with **SAQ 0.26.4** as the task queue substrate, pinned in `pyproject.toml`.

### Worker topology

- **TWO worker processes per machine**, both asyncio-native:
  - **runs worker**: queue=`runs`, concurrency=2, no web UI.
  - **system worker**: queue=`system`, concurrency=2, all cron jobs, web UI on port 8081 bound to `127.0.0.1` (fly-ssh only) via a custom runner (`aiohttp` has no host flag; the plain CLI binds `0.0.0.0`). AUTH required and **fail-closed**: the entrypoint refuses to boot the system worker if `SAQ_AUTH_PASSWORD` or `SAQ_AUTH_USERNAME` are unset.
- `runs_manual` + `runs_automated` collapse into **ONE `runs` queue**. Celery consumes both today via `task_queues`, but the entire worker process is deleted, so the collapse loses nothing.
- Global run concurrency: 2/machine × 2–5 machines = 4–10 concurrent runs.

### Concurrency model (2026-08-05 revision)

Worker concurrency was coupled to the Redis pool size during the cutover
firefight, then decoupled. Current (post-fix) model:

- **`SAQ_WORKER_CONCURRENCY`** (default `5`) is the only worker-concurrency
  knob, decoupled from the Redis pool size. This is a NEW knob: during the
  cutover, the #663 coupling bug meant raising `SAQ_REDIS_POOL_SIZE` 5 → 20 →
  50 silently raised worker concurrency 5 → 20 → 50, multiplying in-flight jobs
  (and therefore DB/Redis load) with every pool resize. Decoupling makes the
  two budgets independent and independently tunable.
- **`SAQ_REDIS_POOL_SIZE`** (default `20`) and **`SAQ_WORKER_DB_POOL_SIZE`**
  (default `10`) were firefight residues: both were raised during the cutover to
  relieve "Too many connections" pressure and were never re-derived from the
  actual connection budgets. **Budget verification CLOSED — 2026-08-06**
  (FAR-88 / the tier ticket). Verified facts from prod (`fly ssh console`):
  deployed Postgres (modulo-app-db, Fly Postgres 17.9) reports
  `max_connections` = **300** with ~**40** connections in use at sample time;
  Upstash showed ~**15** connected clients at sample time and prod pins the
  `SAQ_REDIS_POOL_SIZE` secret to **5** (maxclients is hidden on Upstash).
  Re-derived defaults: `SAQ_WORKER_DB_POOL_SIZE` stays **10** (10 x 2 workers x
  up to 5 machines = 100 + web pools + checkpointer, comfortably under 300) and
  `SAQ_REDIS_POOL_SIZE` is lowered 50 → **20** (workers hold pool conns only
  while running jobs — ~5 jobs x 2 workers = 10 live conns per machine — so 20
  caps at 200 potential conns across 5 machines; operators on a small Redis
  tier may lower to 5, matching prod). Accepted design target: concurrency 5
  per worker x up to 5 machines = up to 25 concurrent runs, verified-safe
  against the 300-connection cap.
- **`max_concurrent_ops` reserve clamp** (SAQ RedisQueue semaphore): must stay
  strictly below the pool size so the semaphore can never exhaust every
  connection. Reserve formula: pool ≤ 1 → `pool`; pool 2–5 → `pool − 1`; pool
  > 5 → `pool − 5`. Implemented as `_max_concurrent_ops()` in
  `backend/src/modulo/core/saq_worker.py` and covered by unit tests.

### Concurrency: product vs infra

The connection-pool knobs (`SAQ_REDIS_POOL_SIZE`, `SAQ_WORKER_DB_POOL_SIZE`,
`SAQ_WORKER_CONCURRENCY`) are **infra-specific server configs**: operators tune
them to their deployment's Postgres/Redis capacity (the verified budget above).
They are not product concurrency controls.

The **product-facing** concurrency control is the number of concurrent
runs/pipelines a customer runs — `Pipeline.max_concurrent_runs` (per pipeline,
default 5) plus the org-level sandbox cap — which customers set to suit THEIR
infra. SAQ pool knobs exist to make the chosen product concurrency fit the
underlying Postgres/Redis; a capacity-blocked run stays `pending` and is
re-dispatched by `dispatcher_reconcile` when capacity frees. No new product
feature is implied here — this is a positioning note only.

**Org-level run admission control (2026-08):** `dispatch_run` additionally
honours an org-wide `run_concurrency_limit` (read from
`Organisation.settings_json`, `None` = uncapped). An org that already has that
many executing/claimed runs across ALL its pipelines is deferred at dispatch
time so one org cannot flood the shared worker pool. A currently-`pending` run
is demoted back to `pending` with the `org_capacity_limited` reason marker and
recovered by the stale-run sweep's stranded-capacity re-dispatch (the same
mechanism as per-pipeline deferrals), never by `never_dispatched`. A `resume_run`
dispatch is NEVER org-cap deferred — a resume is the continuation of an
already-admitted run (it is already `running` and already consumes a slot), so
the gate only applies to NEW run admissions; deferring a resume would 500
`recover_node` and lose the resume payload. The org run cap is ALSO re-checked
at CLAIM time in the executor (`PipelineExecutor._check_capacity`), mirroring
the sandbox-cap backstop: dispatch-time admission counts active runs in one
transaction but enqueues later, and newly-enqueued runs stay `pending`
(invisible to the count) until a worker claims them — so a burst of dispatches
can each see `active < limit` and all enqueue, exceeding the cap by the batch
size. The claim-time re-check demotes the run back to `pending` with the
`org_capacity_limited` marker, closing that TOCTOU window.

### Database as system of record

- The DB remains the system of record for run state. Three columns are added: `dispatcher`, `saq_job_id`, `claim_token`. `re_enqueue_count` is cut (re-enqueue detection moved to log-line ingestion).
- `dispatcher_reconcile` (system cron, every 60s) recovers "DB says job should exist, Redis says none": verifies via `queue.job(run:{id})`, repairs partial eviction with queue-name-derived key deletion, and re-enqueues — gated on the enqueue return value so it never loops.
- Capacity gating is **DB-deferred**: capacity-blocked runs are never enqueued, stay `pending`, and `dispatcher_reconcile` re-dispatches them when capacity frees.

### Delivery sequencing — three PRs

- **PR A**: foundation + spike (hard gate) + tests-first. The SPIKE runs a throwaway worker against an identically-configured dev Upstash instance (never production) and settles the remaining empirical unknowns: Upstash maxmemory-policy, ttl semantics, retry timing, E2B transient-retry distribution. Raw spike evidence will be committed with PR A.
- **PR B**: routing (`dispatch_run` single gating point) + the 3 columns + `saq` error enum + shadow mode + staging smoke. SAQ runs in shadow on production (`SAQ_ENABLED=false`): `execute_run` keeps routing to Celery (`dispatcher=NULL`), `resume_run` routes to SAQ (`dispatcher='saq'`). The staging smoke flips `SAQ_ENABLED=true` on dedicated Upstash + Postgres with queue prefixes `staging-runs`/`staging-system` so the acceptance (`dispatcher='saq'` + `claim_count==1`) is reachable.
- **PR C**: cutover + Celery removal, with a **48–72h hold** (retired 2026-08-05 — see header note; the deploy gate it described no longer exists). Sequenced rollout: deploy the image with BOTH Celery+SAQ and `SAQ_ENABLED=true` on all machines first, verify SAQ green, then deploy the Celery-removal image — never a scheduler-less window.

### At-most-once residual and its mitigations

- The accepted at-most-once residual: an event-loop stall ≥450s freezes the DB heartbeat and `job.update()`, the sweeper re-queues, and a retry can double-execute. Mitigated by two mechanisms:
  - **Claim-token fence**: `claim_token` is a DISTINCT per-claim value (SAQ retries reuse the same `saq_job_id`), verified on heartbeat (cheap early-abort), immediately before the E2B dispatch (the load-bearing site), and before other side-effect commits. The finally-stale write (`heartbeat_at = now() - 8min`) is itself claim-token-fenced so a superseded original cannot stale a successor's fresh heartbeat.
  - **E2B idempotency key** (per-claim, default ON): key `run:{id}:e2b:{claim_token}` set via SETNX before dispatch (atomic — exactly one executor wins), fenced DEL on failure, retained until terminal + upper TTL bound (~8h). Also catches E2B client-level transient retries within one claim.

### Multi-machine cron safety

- Cron jobs run on every machine's system worker. Safety comes from advancing `next_fire_at` **atomically at enqueue time**: a conditional `UPDATE ... WHERE next_fire_at <= now() RETURNING id` picks the due rows, and only the returned rows are enqueued. A second machine's tick sees `next_fire_at` already advanced and skips. Lost-epoch-on-crash (process dies between the atomic UPDATE and the enqueue) is accepted and self-heals on the next tick, with alerting as the closure.

### CI gating

- SAQ integration tests fold into `ci.yml` as jobs, keeping the workflow name "CI: Fast Validation" — `merge-queue.yml` gates on that workflow name because branch protection is unavailable on this repo (HTTP 403, free tier). Jobs run on hosted parallel `ubicloud-standard-2` runners, so CI wall-clock is `max(jobs)` (~40 min). A one-retry wrapper (pytest-rerunfailures) bounds flake churn; a red-after-rerun SAQ job is expected to trigger Branch Fixer.

---

## Consequences

- **Substrate swap, NOT simplification.** SAQ earns its keep on async transport, timeout-kill, and verified retry/sweeper/cron/dedupe primitives. The bespoke DB glue — `dispatcher_reconcile`, capacity deferral, `after_process`, claim-token fencing — is substrate-independent: it is the price of keeping the DB as the system of record, and it carries over to any future queue substrate.
- **Schema:** 3 columns (`dispatcher`, `saq_job_id`, `claim_token`) + a permanent `'saq'` value added to the error enum (with constraint UPDATE and validator). Two migrations in PR B; **no PR C migration**. The enum value is permanent because PostgreSQL cannot drop an enum value still referenced by rows; rows would be reclassified to `'internal'` if it were ever removed.
- **CI wall-clock** rises to ~40 min `max(jobs)` with hosted parallel runners; the stale AGENTS.md claim ("13 workflows, self-hosted runner, concurrency group") is corrected in the same PR set.
- **The SPIKE (PR A hard gate)** settles the remaining empirical unknowns before production exposure: Upstash `maxmemory-policy` (volatile-lru vs allkeys-lru decides whether the full partial-eviction machinery is load-bearing), ttl semantics (enqueue- vs start-origin decides `execute_run` ttl of ≥8h vs 300s), retry timing determinism (`retry_backoff=False`), and the E2B transient-retry distribution (sets `SAQ_RUN_RETRIES` at ~p99.5).
- **Rollback** (post-PR-C, ordered): halt new dispatches → drain/fail SAQ jobs (never re-dispatch running rows onto old Celery unguarded) → SQL-flip `dispatcher=NULL` + `dispatched_at=NULL` as the owner role (RLS-safe, count-asserted) → run the 3 column down-revisions → deploy the pre-cutover image SHA. Recover from DB state, not queue state. 48–72h green before anything is considered unreversible.
- The at-most-once residual on ≥450s event-loop stalls is acknowledged as residual, not resolved — mitigated (fence + idempotency key + `claim_count` alert), bounded (retries=5), and unlikely for an I/O-bound workload.

---

## References

- Full plan v10: [`docs/adr/plans/celery-to-saq-plan-v10.md`](plans/celery-to-saq-plan-v10.md)
- SAQ 0.26.4 source semantics verified during plan review (retry off-by-one, sweeper, cron, dedupe, web bind, CLI invocation)
- Delivery-plan tasks (historical — tracker retired, see header notice): `task-saq-migration-pr-a`, `task-saq-migration-pr-b`, `task-saq-migration-pr-c`
- ADR 012: Managed Postgres Migration — staging dedicated database (`modulo-staging-db`)
