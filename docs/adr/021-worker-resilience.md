# ADR 021 — Worker machine resilience: `always` restart + process-group health check

**Date:** 2026-08-08
**Status:** Accepted

---

## Context

On 2026-08-08 a rolling deploy to `app.modulo.run` (`[deploy] strategy = "rolling"`)
left **both worker machines in the `stopped` state at ~10:52Z**. Nothing
auto-recovered them for ~3 hours; a human had to run `fly machines start`
manually. During that window the entire SAQ run queue froze — no pipeline runs
processed (runs failed with `NodeCancelledError`/`TypeError` during the outage).

Before this ADR the worker process group was configured as:

```toml
[[restart]]
  processes = ['worker']
  policy = "on-failure"
```

and the worker group had **no health check of any kind** (the app group has
`/healthz/ready`; the worker group had nothing).

### Why `on-failure` did not recover the machines

Fly's restart policies (config reference, "The `restart` section"):

- `always`: "we'll attempt to restart the machine **no matter the exit code**."
- `on-failure`: "we'll only restart the machine if it exited with a **non-zero**
  exit code (due to a failure or crash)."
- A machine that ends up `stopped` after a clean exit (exit 0) is **not**
  restarted by `on-failure` — `stopped` means "exited, either on its own or
  explicitly stopped" (Machine states doc), and the rolling deploy's update
  transition can land a machine in `stopped` ("the new version ... may transition
  to `stopped`, depending on how the update was triggered").

So a crash-looping worker (non-zero exit) is recovered by `on-failure`, but a
worker a deploy leaves `stopped` (clean SIGTERM teardown / clean exit) stays
down indefinitely. The 2026-08-08 outage was exactly that second case, and no
health check existed to even *observe* it.

Also verified from Fly's health-checks docs: **health checks do not auto-restart
machines** — a failing service check only removes a machine from routing, and a
top-level `[checks]` check is observability-only ("for internal monitoring and
alerting ... they don't influence routing"). So a health check alone is NOT a
self-healing mechanism; the restart policy is.

## Decision 1 — Worker restart policy: `always`

Change the worker `[[restart]]` policy from `on-failure` to `always` in
`fly.toml` and `fly.staging.toml`. `always` restarts the machine regardless of
exit code, which is exactly what a long-running background worker wants and is
Fly's documented policy for such workloads.

This closes the observed gap directly: a worker machine left `stopped` by a
rolling deploy (or any crash/clean-exit) is restarted by Fly's scheduler without
human intervention. The deploy workflow's "Ensure worker machine exists" step
remains as a deploy-time belt-and-braces safety net.

Notes:

- The entrypoint's in-container sliding-window crash guard (crash-limit →
  fail-closed exit 1) still works; `always` restarts the container even after
  that deliberate non-zero exit. The guard's job is to bound *how fast* a
  crash-loop is retried, not to keep the container down.
- `fly.demo.toml` has no worker process group (single `app` group), so no change
  applies there.

## Decision 2 — Worker process-group health check (observability + deploy readiness)

Add a top-level `[checks.worker_health]` HTTP check scoped to
`processes = ['worker']` in `fly.toml` and `fly.staging.toml`, and a tiny
liveness server in the `deploy/fly/entrypoint.sh` worker branch that backs it:

- The entrypoint starts a stdlib-only `http.server` on `0.0.0.0:8082` that
  returns **200 only while both SAQ worker subprocesses are alive** (checked via
  the PID files the wrappers write: `/tmp/run-worker.pid` and
  `/tmp/system-worker.pid`), and **503 otherwise**.
- The check is deliberately strict: a worker machine whose container is up but
  whose SAQ workers are dead (or restart-backing-off) is *not* healthy. This is
  the liveness signal the worker group lacked entirely.
- Per Fly docs this is observability, not routing control — `fly checks list`
  now shows real worker liveness, and rolling deploys gain a worker readiness
  signal so a deploy that cannot bring up SAQ workers is visible (and haltable
  via `machine_checks` later) instead of passing silently as it did on
  2026-08-08.
- The health server is launched as a disowned background subshell whose exit is
  always 0 (`|| true`), so it can never fail the container via `set -e`/`wait`,
  and the main `wait` still tracks only the two SAQ wrapper jobs (preserving the
  fail-closed crash-limit path). `kill 0` in the SIGTERM/SIGINT trap tears it
  down on deploy.

### Why not rely on a health check to self-heal

Per Fly docs, checks do not restart machines. The self-healing mechanism is the
restart policy (Decision 1); the check provides the observability and deploy
readiness. The two together mean: a stopped worker self-heals (`always`), and a
silently-dead worker is at least visible and can be gated at deploy time.

## Consequences

- A worker machine left `stopped` by a deploy, crash, or clean exit is restarted
  by Fly automatically — no human `fly machines start`.
- `fly checks list` now reports worker liveness (port 8082) for the worker
  process group.
- One new port (8082) on worker machines; 8081 (SAQ web UI, 127.0.0.1-only)
  is untouched. The app process group is unchanged (its `policy = "never"` +
  `/healthz/ready` LB watchdog design is unaffected).
- Residual risk: `always` will also restart a machine after a *deliberate*
  container stop that is not an explicit API stop; this matches the intent that
  workers should always be running. An explicitly stopped machine (`fly machine
  stop`, `flyctl scale count 0`) is not auto-started by Fly regardless of policy.
