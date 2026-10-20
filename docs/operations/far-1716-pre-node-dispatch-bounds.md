# FAR-1716 – run-level dispatch bounds: the pre-node window has no run-level deadline

This note corrects a recurring wrong mental model: **"the run has a 55-minute
cap."** It does not. The familiar `timeout_seconds` value on a `sandbox_agent`
node (e.g. 3300 s / 55 min) is a **per-node** cap measured **from node start**.
Before the first node starts, there is no run-level deadline at all - the
pre-node window is bounded only by separate watchdog/repair mechanisms, and a
zero-node run therefore legitimately lives ~35–135 minutes under current
defaults. Every claim below cites the exact code verified at HEAD.

## 1. `timeout_seconds` is per-node, measured from node start

`execute_run`'s node-deadline watchdog tracks each in-flight node independently
(`backend/src/modulo/core/pipeline_execution.py:1850-1876`). The deadline entry
is created only when a node **starts**:

```python
def _on_node_started(nid: str) -> None:
    to = node_timeouts.get(nid, default_timeout)
    node_deadlines[nid] = (time.monotonic() + to, to)
```

(`pipeline_execution.py:1870-1873`), wired to the executor's `node_started`
stream event (`pipeline_execution.py:1879-1880`; the executor fires the callback
at `pipeline_engine/executor.py:6746-6747`). The default timeout for nodes with
no explicit value is capped at `MAX_NODE_TIMEOUT_SECONDS = 3300`
(`settings.py:27`, read at `pipeline_execution.py:1934`).

Consequence: a long pre-node dispatch delay (queue wait, claim churn,
re-dispatch cycles) does **not** consume any node's `timeout_seconds` - the
clock starts when the node actually starts.

## 2. Before the first node starts there is no deadline at all

`node_deadlines` is empty until the first `node_started` event, so nothing in
that dict bounds the pre-node window. Three separate mechanisms do, none of
which is the node timeout:

### (a) `zombie_watchdog` - per attempt, at `saq_setup_grace_seconds` (default 600 s)

`zombie_watchdog` (`pipeline_execution.py:1318-1373`) waits up to
`SAQ_SETUP_GRACE_SECONDS` (default 600 s, `settings.py:32`, `settings.py:570-571`)
for first progress (first node dispatched). If the window elapses with zero node
progress it cancels the executor and fails the run `executor_stalled` -
**unless** the run's retry policy covers `stall`, in which case the kill first
consults the shared watchdog-retry mechanism and **re-dispatches** the run
instead of terminal-failing it (`pipeline_execution.py:1349-1359`,
`1420-1426`). This watchdog runs **per attempt** (one `execute_run` job), so a
re-dispatched attempt gets a fresh grace window.

### (b) `dispatcher_reconcile` nodeless repair - at `saq_claimed_nodeless_minutes` (default 35 min)

The `dispatcher_reconcile` system cron's nodeless branch matches
`running` + `dispatcher='saq'` + zero node progress older than
`SAQ_CLAIMED_NODELESS_MINUTES` (default 35, `settings.py:634`; predicate at
`cron_helpers.py:4586-4674`). The repair is **re-dispatch-first**: the
four-way outcome in `_reconcile_nodeless_repair`
(`cron_helpers.py:8286-8385`) terminal-fails **only** when the retry budget is
exhausted (`_should_redispatch_nodeless` False, `cron_helpers.py:8335-8346`);
otherwise it re-dispatches (throttled to at most one re-dispatch per 35-min
window per run, `cron_helpers.py:8347-8357`). Re-dispatch is safe here because a
nodeless zombie executed zero nodes by construction - enforced by both the SQL
predicate and the row-level recheck (`cron_helpers.py:4677-4733`).

### (c) Unconditional zero-progress age bound (~135 min)

Independent of any retry policy or budget, the reconcile cron's age gate
terminal-fails any `running` + `dispatcher='saq'` row older than
`_MID_GRAPH_WEDGE_MAX_AGE_MINUTES`
(`cron_helpers.py:303-318`, applied at `cron_helpers.py:6534-6546`):

```python
_MID_GRAPH_WEDGE_MAX_AGE_MINUTES = max(SAQ_RUN_TIMEOUT // 60, 120) + 15
```

With `SAQ_RUN_TIMEOUT = 7200` s (`core/dispatch.py:34`) that is
`max(120, 120) + 15 = 135` minutes. Zero-progress rows (no node attempts, no
token usage, no `__final__` output, no checkpoints) go through the nodeless
router's own terminalizer; rows with node progress are failed `no_progress`.

**FAR-1803 caveat (2026-10-15).** The claim "the ~135-minute age bound
terminal-fails it regardless" was FALSE for one shape: a node-STARTED row
(`dispatch_phase='first_node_dispatched'`) carrying a LIVE dispatch marker
(`{"state": "dispatching", ...}`). The age gate's FAR-1740 (3) live-marker
arm shielded such a row from the gate **regardless of whether the executor
that wrote the marker was still alive**, so when the executor died mid-dispatch
(heartbeat stale, zero node outputs), the row sat `running` holding a
sandbox-concurrency slot, invisible to every reconcile counter, until the
25-hour runner-marker sweep. Observed on prod 2026-10-14/15 (runs 35905,
36255, 36269, 36274, 36278, 36283, 36325-36327). Fixed by FAR-1803: the
shield now applies only while `heartbeat_at` is fresh (a stale/NULL heartbeat
proves the owning attempt is gone), the reconcile scan admits the class under
a dedicated leg counting it `node_started_executor_lost` (never re-dispatched;
FAR-1649), and the age gate collects it `no_progress` at the bound.

## 3. Therefore a zero-node run legitimately lives ~35–135 minutes

Under current defaults, a run claimed by SAQ that executes no node is
re-dispatched by (b) on a 35-minute cycle (budget `SAQ_NODELESS_REDISPATCH_BUDGET`,
default 4, `cron_helpers.py:4861`), with each attempt bounded by (a)'s 600 s
grace, until the (c) ~135-minute age bound terminal-fails it regardless. A
zero-node run sitting in `running` for an hour is **not** a diagnosis of a
55-minute cap being ignored - no such cap applies to it. A node-STARTED run
whose executor dies mid-dispatch is the same ~135-minute bound (FAR-1803,
above): held and counted as `node_started_executor_lost` from the first
stale-heartbeat tick, terminalised `no_progress` at the bound.

## 4. Timestamp semantics: `started_at` vs `dispatch_phase_entered_at`

- `runs.started_at` is stamped **once at the first claim and never refreshed**
  on re-dispatch (`cron_helpers.py:4565-4566`, in the `_per_attempt_anchor_expr`
  docstring). The per-attempt clock is `dispatched_at`, rewritten by every
  dispatch (`cron_helpers.py:4571-4575`).
- `dispatch_phase_entered_at` is stamped `now()` in the **same UPDATE that
  claims the row**, on **every** claim - execute re-claim and HITL resume alike
  (`pipeline_execution.py:551-578`).

So the measured gap `dispatch_phase_entered_at − started_at` is a
**multi-attempt sum** (all earlier attempt time plus the current claim), not one
queue wait. Do not read it as "how long this attempt waited."

## 5. The 5-minute `stale_run_recovery` sweep gives SAQ runs nothing

The legacy `stale_run_recovery` sweep runs every 5 minutes on the system
worker, but its never-dispatched and worker-lost branches are deliberately
scoped to non-SAQ rows:

- never-dispatched: `AND (dispatcher IS NULL OR dispatcher != 'saq')`
  (`pipeline_execution.py:2388`)
- worker-lost: same exclusion (`pipeline_execution.py:2481`)

as documented in the sweep's docstring ("SAQ runs never carry
worker_lost/never_dispatched", `pipeline_execution.py:2581-2585`). The
remaining branches concern capacity-deferred **pending** runs, not claimed
SAQ runs. SAQ run recovery is owned entirely by (a)/(b)/(c) above - the
5-minute sweep is not a bound on a stuck SAQ run and must not be cited as one.
