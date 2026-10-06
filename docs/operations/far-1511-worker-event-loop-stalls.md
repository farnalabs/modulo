# FAR-1511 — prod worker event-loop stalls and `dispatcher_reconcile` 95s timeouts

Investigation record for the report that the production worker machine still
shows residual 0.6–1.2 s event-loop stalls and `dispatcher_reconcile` inner
deadline timeouts after FAR-1439 fixed the big ~2.4 s readiness-probe stall.

Everything below is either a direct observation (a command output, a log line,
an HTTP response) or an inference that is labelled as one. Times are UTC on
2026-10-05.

## The shape of the deployment under test

Observed with `flyctl machine status` (read-only):

| | app machine | worker machine |
|---|---|---|
| process group | `app` | `worker` |
| CPU | 4 shared vCPU | **1 shared vCPU** |
| memory | 2048 MB | 2048 MB (+1024 MB swap) |
| state | started | started (a second worker machine is a stopped cold standby) |

The readiness payload on the app machine confirms one machine serves everything
on the worker side:

```
saq_workers: ok — saq workers live on all queues
              (fleet: {'runs': {'<worker>'}, 'system': {'<worker>'}})
```

So the single shared-cpu-1x worker runs the `runs` SAQ worker
(`SAQ_WORKER_CONCURRENCY=20`), the `system` SAQ worker (every system cron,
including `dispatcher_reconcile` and `fire_due_triggers` on a 60 s cadence),
and the port-8082 liveness server.

## What was observed

### 1. The 95 s budget is spent inside one organisation's reconcile pass

Sampled `GET https://app.modulo.run/healthz/ready` seven times while the
`dispatcher_reconcile` check reported a timeout tick. Every sample carried the
same shape:

```
dispatcher_reconcile fresh but status=timeout
  (last_run_at=..., last_error=inner deadline after 95.3s (budget=95s,
   max_rows=500) during stage=reconcile_org:<org-id>: TimeoutError: );
  scanned=3, repaired=0, skipped=2, ... rows_deferred=0
```

Two things follow. The tick never leaves `reconcile_org:<org>` — it does not
reach `record_facts`, `compensating_sweeps`, or the stats write before the
deadline — and the counters (`scanned=3, repaired=0, skipped=2`) are identical
across all seven samples, so the tick makes the same amount of progress each
time and then stops at the same place. The budget is not spread across work; it
is consumed by one await inside one organisation's pass.

A tick that succeeds looks completely different (`scanned=5, repaired=2,
skipped=3, status=ok`), so the pass itself is normally fast.

### 2. The database and Redis are fast throughout, from the app machine

While a reconcile tick was in its 95 s window, `healthz/ready` was polled every
5 seconds for 4 minutes from the app machine — 50 samples spanning a full
timeout:

```
18:20:50  appDB=51.0ms   appLag=8.4ms   dr=stale106
...
18:23:06  appDB=55.2ms   appLag=13.7ms  dr=stale242
18:23:11  appDB=64.9ms   appLag=12.5ms  dr=TIMEOUT
...
18:24:09  appDB=48.9ms   appLag=10.0ms  dr=TIMEOUT
18:24:14  appDB=58.2ms   appLag=12.4ms  dr=stale63
```

Database latency never left 24–65 ms and the app's own event-loop lag never
left 4.6–18.2 ms, including at the exact moment the worker's tick gave up. The
same Postgres answers the app in well under 100 ms while the worker's reconcile
cannot finish a pass in 95 s.

### 3. The worker machine's own database connections are failing

Over a 4-minute capture of the worker machine's log during one of those windows
(5600 log lines):

| observation | count |
|---|---|
| `asyncpg.exceptions.ConnectionDoesNotExistError: connection was closed in the middle of operation` | 9 |
| `ConnectionResetError: [Errno 104] Connection reset by peer`, raised inside SQLAlchemy's connection `create` (i.e. while **establishing** a connection) | 1 |
| `dispatch_phase.write_timeout run=... phase=... timeout=2.0s (dropped)` | ~40 |
| `Heartbeat failed for run ... (N consecutive)` | 7 |
| `run_context.cancellation_db_timeout` | 7 |
| `sqlalchemy.exc.DBAPIError ... connection was closed in the middle of operation` | 10 |

`dispatch_phase.write_timeout` is a deliberately bounded 2-second write, so
roughly ten of them per minute means the worker's own database round-trips are
routinely taking longer than 2 seconds during that window.

### 4. The system worker's own readiness cron detects stalls

The `health_readiness_alert` system cron runs the same readiness evaluation the
public endpoint runs, inside the system worker process. It logged:

```
17:35:03  health._check_db_hygiene → TimeoutError   (1 s budget)
17:35:07  health.readiness event-loop stall detected (FAR-1439)
18:00:06  health._check_db_hygiene → TimeoutError
18:00:09  health.readiness event-loop stall detected (FAR-1439)
18:10:06  health.readiness event-loop stall detected (FAR-1439)
18:15:07  health._check_db_hygiene → TimeoutError
18:15:07  health.readiness event-loop stall detected (FAR-1439)
```

`_check_db_hygiene` is a single read of `pg_stat_user_tables` +
`pg_database` through the pooled engine, with a 1-second budget. The same check
takes 247 ms on the app machine (Fly's own check output) and the app's
`event_loop_lag` check reports 5–18 ms. So the check is neither expensive nor
slow on the server — it is slow *in the system worker process*.

### 5. The failures track run execution, not the clock

- 17:53–17:58 (no pipeline-run activity in the worker log): every system cron
  held its exact cadence — `dispatcher_reconcile` completed at 17:53:36,
  17:54:37, 17:55:37, 17:56:37 — and no deadline fired.
- 17:30–17:35, 17:59–18:04, 18:10–18:16 and 18:20–18:24 (runs actively
  executing): `dispatch_phase.write_timeout` fires continuously, the
  `event-loop stall detected` warnings appear, and `dispatcher_reconcile`
  misses ticks — including one gap where no tick completed for 242 seconds.

### 6. What could NOT be measured

Machine CPU and memory percentages were **not obtainable** with the read-only
Fly token available to this investigation:

- `flyctl metrics` in this build is the metrics-*ingest* subcommand, not a
  viewer (`flyctl help`: "Commands that handle sending any metrics to
  flyctl-metrics"), and `flyctl help` prints
  `Warning: Metrics token unavailable: no metrics token in config`.
- The GraphQL schema for the app exposes no metrics field
  (`__type(name:"App")` / `__type(name:"Machine")` field lists contain none).
- `api.fly.io/prometheus/api/v1/query` returns **401** for this token;
  `api.fly.io/api/v1/apps/<app>/metrics` and
  `api.machines.dev/v1/apps/<app>/machines/<id>/metrics` return **404**.

So "the box is CPU-saturated" is an *inference*, not a measurement. A
`memory_monitor_cron` guest-memory alarm (`worker.guest_memory_alarm`) did not
appear in ~55 minutes of captured worker logs, which weakly suggests memory was
not the binding constraint during the window; that cron only logs an INFO line
hourly when healthy, so its silence is not proof.

## What the observations rule out

- **A synchronous or CPU-heavy call on a job or cron path.** No traceback in any
  capture names a blocking frame. Every failure is an `await` that does not
  return in time: `asyncio.wait_for` timing out, `asyncio.timeout(95)` firing,
  or an asyncpg call on a connection that was closed. FAR-1439's sync Alembic
  parse is already off the loop (threaded) and process-cached.
- **Database or Redis latency.** Sample 2 above: 24–65 ms to the same Postgres
  from the app machine for the whole duration of a worker-side 95 s stall.
- **A memory alarm.** None fired during the capture (weak evidence — see §6).

## What remains open

Two worker-side explanations fit the evidence, and this investigation could not
separate them:

1. **CPU contention.** One shared-cpu-1x runs two SAQ workers at concurrency 20
   plus every system cron, while the web machine gets 4 shared CPU. Under run
   load, the worker's event loop is delayed (`event-loop stall detected`,
   1 s sub-check timeouts, 2 s bounded writes failing).
2. **Worker → Postgres connection loss.** Connections are closed mid-operation
   and reset during establishment (sample 3) while the app's connections to the
   same database stay healthy. That points at connection churn, a per-source
   connection cap, or a proxy/pooler limit on the database path — none of which
   is visible from read-only Fly tooling.

Distinguishing them needs either a Fly metrics token (CPU/memory series) or
`pg_stat_activity` / connection-count history on the database — both outside
this investigation's access. Note that they are not mutually exclusive: a
process that is both starved of CPU and losing its connections produces exactly
the observed mix.

## Change made

`fly.toml` declares the worker `[[vm]]` block at **2 shared CPU** (was 1), with
the observation record above kept in the file comment. Rationale: the worker
group carries the whole background fleet on the smallest VM in the app while
the web machine carries only HTTP on four, the failures track run load rather
than the clock, and the same database is fast from the other machine
throughout. Declaring the size makes the intended scale reproducible from the
repository instead of living only on the machine.

`[[vm]]` applies to new machines only, so the declared size reached the live
machine only when a later deploy recreated it: the started worker is now live at
**2 shared CPU** (deploy run 37414967179), so no resize step is outstanding for
it. The stopped cold standby is an existing machine too — starting it does not
apply the declared size, so it keeps its recorded 1-CPU config until it is
updated (`flyctl machine update <standby-machine-id> -a app-modulo --vm-size
shared-cpu-2x`, safe while stopped) or recreated by a deploy. The app machine
needed no such step — it was observed at 4 shared vCPU on 2026-10-05.

The cadence gap described above (no per-organisation **time** bound, only a row
budget) was closed by FAR-1525 (PR #1342): each org's reconcile pass now runs
under its own timeout at `DISPATCHER_RECONCILE_ORG_BUDGET_SECONDS` (default 30,
min 1, max 119), clamped to the tick's remaining budget minus a 15 s tail
reserve for facts/sweeps. On expiry the org's transaction rolls back, a truthful
`status='timeout'` / `org_timeouts` marker names the org, and the loop
continues — one hung org can no longer consume the whole inner deadline.

## Follow-ups

- **Done (FAR-1525, PR #1342):** a per-organisation **time** bound inside
  `dispatcher_reconcile`. The row budget from FAR-1425 caps *how much* work one
  organisation can do, but a single hung await could still eat the whole
  95-second deadline (the seven timeout ticks above) — now each pass is cut at
  `DISPATCHER_RECONCILE_ORG_BUDGET_SECONDS` (default 30 s), clamped to the
  tick's remaining budget minus a tail reserve for facts/sweeps, and the tick
  records a truthful timeout marker and continues.
- **Needs human:** obtain a Fly metrics token (or read the dashboard) and
  confirm CPU/memory saturation on the worker during a run-heavy window; if it
  is not saturated, the connection-loss hypothesis (item 2 above) becomes the
  primary lead and needs database-side connection counts.
- **Needs human:** the stopped cold standby still carries its recorded 1-CPU
  config; when it is started, either run `flyctl machine update
  <standby-machine-id> -a app-modulo --vm-size shared-cpu-2x` (safe while
  stopped) or let a deploy recreate it. The started worker needs no resize.
- **Considered and not done:** splitting the system-cron worker onto its own
  process group so cron scheduling cannot be delayed by run execution. It is the
  cleaner structural fix for "system crons share a machine with the runs queue",
  but it adds a standing machine to a budget that the `fly.toml` machine block
  explicitly guards, so it is a cost decision rather than a code decision.
