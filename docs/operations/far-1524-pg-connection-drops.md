# FAR-1524 – Postgres connection drops in run-heavy windows: findings & fix

**Status:** attribution + app-side hardening delivered; one Fly-side sub-class
escalated as needs-human. Date: 2026-10-07. Read-only prod investigation –
no prod state was mutated.

## Symptom (as reported from prod worker logs)

During run-heavy windows:

1. `asyncpg.ConnectionDoesNotExistError: connection was closed in the middle of operation`
   raised mid-operation.
2. The same error (and `ConnectionResetError: [Errno 104]`) raised **inside
   SQLAlchemy's connection create** (establishment).
3. `dispatch_phase.write_timeout ... timeout=2.0s (dropped)` warnings.
4. Heartbeat failures (one run terminal-failed `executor_heartbeat_lost`).

## Attribution

### Decision

**Dominant cause: (b) – the proxy in front of Postgres closing long-lived
connections, specifically HAProxy's 30-minute session inactivity window
(`timeout client 30m` / `timeout server 30m` in the DB machine's
`/fly/haproxy.cfg`), killing sessions whose operation has been pending in
silence for ≥30 minutes – with `pool_recycle=3600` (60m) > 1800s as the same
defect class at the pool layer.** The competing candidates (a), (c) and (d)
are **refuted for the error window by observation** (below).

### Observed (each with its evidence)

| # | Observation | Source |
|---|---|---|
| O1 | `timeout client 30m`, `timeout server 30m`, `timeout connect 4s`, `retries 2`, `on-marked-down shutdown-sessions`, `http-check expect string primary` – the full `/fly/haproxy.cfg` read from the DB machine | prod SSH, 2026-10-07 |
| O2 | HAProxy stats (CSV + HTML), process uptime 5d8h → covers **all** error episodes (Oct 3–5): `chkfail=0` (zero failed health checks), `chkdown=1` (startup only), `downtime=0s`, `econ=0` (zero backend dial failures), queue `0/0`, `dreq/dresp=0`, `maxconn reached=0`, connect time avg 0 ms / max 25 ms, 156 *client*-side connection resets during transfers (0 server-side) | prod SSH → `http://127.0.0.1:8404/stats;csv` |
| O3 | Postgres server: `idle_session_timeout=0`, `idle_in_transaction_session_timeout=0` (server never closes idle sessions), `max_connections=300` with ~47 in use, `pg_postmaster_start_time = 2026-10-02` (no restart during the error window) | `SHOW` / `pg_postmaster_start_time()` on prod |
| O4 | All `modulo_app` connections arrive from the DB machine's own 6PN address → they go through HAProxy on :5432 (the gathered topology claim re-verified) | `pg_stat_activity.client_addr` |
| O5 | 71 runs failed in 30 days with the exact `connection was closed in the middle of operation` text – 11 on Oct 3 (21–23 h), 59 on Oct 5 (12–22 h), clustered inside the **run-heavy hours** (Oct 5 20:00 = 47 runs/h, 22:00 = 71 runs/h). Same minutes also contain `deadlock detected` victims on `runs`/`organisations` | `runs.error_detail` / `runs.error_code` |
| O6 | One `DBAPIError` whose traceback bottoms out at asyncpg `connect_utils.__connect_addr → await connected` raising `ConnectionDoesNotExistError` – i.e. the **establishment** leg, inside `pool._checkout` (Oct 5 18:19); ~6 further establishment failures inside the LangGraph checkpointer's own `AsyncConnection.connect` | `runs.error_detail` |
| O7 | Zero `QueuePool` (pool-timeout) failures, zero `too many clients`, zero `Errno 104` strings in `runs.error_detail` over 30 days | prod SQL |
| O8 | **Zero** `connection is closed` / `InterfaceError`-family failures in 30 days: every surfaced connection death happened **while an asyncpg operation was pending** | prod SQL |
| O9 | Code (pre-fix): `pool_recycle=3600` hardcoded in `db/session.py`; `pool_pre_ping=True` | this repo |
| O10 | Library semantics: SQLAlchemy recycles a pooled connection when its **age** exceeds `pool_recycle` (at checkout); pre-ping failures are classified as disconnects (`asyncpg` dialect `is_disconnect` → `connection.is_closed()`) and silently masked at checkout; asyncpg raises the exact observed message only from `_handle_waiter_on_connection_lost` – transport lost **while a waiter was pending** | sqlalchemy 2.1.3 / asyncpg 0.31 source in the venv |
| O11 | Live PG log (2026-10-07, run-heavy hour): `ERROR: canceling statement due to user request ... while locking tuple in relation "runs" / STATEMENT: UPDATE runs SET dispatched_at=now() WHERE id=$1` – `runs` row-lock waits are real during heavy windows | `fly logs --app modulo-app-db` |

### Inference (reasoned from the observations above – not directly caught)

- **I1 – mechanism of the mid-op errors.** The exact message requires the
  transport to die with an operation pending (O10). For the error window,
  every external kill candidate is refuted by counters (d: `chkfail=0`,
  `chkdown=1` startup-only; c-at-proxy: `econ=0`, queue `0/0`, sub-25 ms
  connects; server-side idle close: `idle_session_timeout=0`; restart: O3).
  The one kill mechanism still present in the config is HAProxy's 30-minute
  **inactivity** timeout: a session carrying an operation that has been
  pending in silence for ≥30 min (a blocked statement on a `runs` row lock –
  O11 – or any unbounded writer awaiting a wedged holder) is closed, the
  pending waiter receives `ConnectionDoesNotExistError` (O10), and the proxy's
  own counters stay clean because an inactivity close is a *normal* session
  close. The minute-level pattern fits: the Oct 5 20:48 cluster (kills at
  20:48:00/15/20 next to deadlock detections at 20:47–20:48:24) is what a
  lock pileup that formed ~30 minutes earlier looks like when its waiters get
  culled together. **Gap (labelled):** no `pg_stat_activity` history was
  captured during a window, so the ≥30-minute pending wait itself was not
  directly observed – this is the strongest inference the available evidence
  supports, not a reproduction.
- **I2 – why it lands in run-heavy windows.** Heavy windows pile row-lock
  waiters (O5/O11) and hold connections quiet across long node executions;
  both produce the ≥30-minute silence the proxy needs. The `write_timeout`
  and heartbeat warnings (item 3/4 of the symptom) follow from the same
  wedge: connections parked in lock waits drain the worker pool
  (`max_overflow=0`), so a phase write's checkout can exceed its 2-second
  fail-soft bound – pool contention here is **downstream of the wedge, not an
  independent pool mis-sizing** (supported by O7: zero pool-timeout failures
  in 30 days). The 2-second bound was left untouched – it is working as
  designed (best-effort instrumentation, dropped not raised).
- **I3 – the pool layer.** `pool_recycle=3600` (O9) exceeds the proxy window
  by 2×: pooled connections could outlive it. Idle-in-pool deaths are masked
  by pre-ping at checkout (O10) – so this mismatch is a churn amplifier, not
  the direct source of the 71 mid-op errors (which per O8 all happened with
  an operation pending). It is still a real config defect and the mandated
  hardening: with recycle inside the window, a pooled connection is closed by
  SQLAlchemy *before* the proxy can strand it.

### Refuted for the error window (observed, not inferred)

- **(d) HAProxy `on-marked-down shutdown-sessions` health-check flap** –
  `chkfail=0`, `chkdown=1` (startup only), `downtime=0s` across the whole
  5d8h HAProxy lifetime covering every error episode (O2). The shutdown
  machinery never fired.
- **(c) connect-timeouts at the HAProxy hop** – `econ=0`, queue never
  non-zero, connect times ≤25 ms (O2).
- **Server-side idle/session closes** – `idle_session_timeout=0`,
  `idle_in_transaction_session_timeout=0`, no Postgres restart (O3).
- **(a) worker pool exhaustion as the driver** – zero `QueuePool`
  pool-timeout failures in 30 days of run failures (O7); pool contention
  appears only as the downstream effect of the wedge (I2).

### App-side vs Fly-side split

- **App-controllable (fixed here):** `pool_recycle` – was hardcoded 3600 >
  the proxy's 1800; now `DB_POOL_RECYCLE_SECONDS`, default 1500 (25 m),
  fail-fast bounded `ge=60, lt=1800` so the mismatch cannot be
  reintroduced; `pool_pre_ping` stays on.
- **App-controllable (follow-up, outside this branch's allowlist):**
  bound the remaining unbounded `runs`-row writers with `SET LOCAL
  lock_timeout` (repo precedent: `mutation_row_lock_timeout_ms`,
  `runner_capacity_lock_timeout_ms`, `saq_hooks`) so no writer can ever sit
  pending ≥30 minutes; audit why a `runs` holder wedges long enough to build
  a pileup (idle-in-transaction holders are never reaped server-side).
- **Fly-side / needs-human:** the **establishment** sub-class (O6) –
  asyncpg handshake closures during `pool._checkout` and checkpointer
  connects – is explained by **none** of HAProxy's counters (`econ=0`,
  `chkfail=0`, queue `0/0`) and did not correspond to a Postgres restart.
  The closure therefore happened on the app↔proxy (6PN) leg or during PG
  startup handling in a way no available counter sees. Needs a Fly-side
  investigation (6PN packet captures / Fly support) – do not invent an
  app-side workaround for it: there is no app-side evidence to justify one.

## Fix delivered in this branch

- `settings.py`: new `db_pool_recycle_seconds` (`DB_POOL_RECYCLE_SECONDS`,
  default 1500, `ge=60, lt=1800`) with the HAProxy 30 m relationship
  documented at the field.
- `db/session.py`: `_build_engine` reads the setting instead of the
  hardcoded 3600; `pool_pre_ping=True` unchanged.
- **Engine-build sweep (follow-up commits):** every remaining long-lived
  pooled engine now honours the same setting –
  `api/dependencies.py` (`_SYSTEM_ASYNC_ENGINE`, system role),
  `core/reports/scheduler.py`, `core/error_tracking/saq_hooks.py`,
  `core/saq_worker.py` (`_get_system_async_engine`),
  `core/cron_helpers.py` (`_get_system_engine`) and
  `cli/break_glass.py` (`get_break_glass_engine` – module-cached, so it can
  outlive a single command) – the last three of which previously passed
  **no** `pool_recycle` at all, i.e. age-unbounded pooling, strictly worse
  than the 3600 s mismatch. A `pool_recycle` grep over `backend/src/**`
  shows **every** code site reading `settings.db_pool_recycle_seconds`
  (0 exceptions).
- `docs/configuration-reference.md`: the new knob documented.
- Tests: `tests/unit/db/test_session.py` gains the proxy-window contract
  (default below 1800, configurable, pre-ping on, Settings fails fast
  at/above the window); per-site pins added in
  `tests/unit/api/test_dependencies_gates.py`,
  `tests/unit/reports/test_report_scheduler.py`,
  `tests/unit/error_tracking/test_saq_hooks.py`,
  `tests/unit/core/test_saq_worker.py`. The pre-existing pin in
  `tests/unit/test_dependencies.py` was updated (adjudicated) and two
  settings-stub classes in `test_dependencies_gates.py` gained the new
  field.
- Not applicable to the sweep (no pooled connection can outlive the
  window): the `NullPool` engines (`db/crud/pipeline.py`,
  `db/crud/pipeline_snapshot.py` – connections live exactly one
  operation), and the one-shot CLI/diagnostic engines that build a fresh
  engine per process (`cli/users.py`, `launcher/doctor.py` – process
  lifetime ≪ 1800 s). `cli/break_glass.py` was previously listed here, but
  it caches its engine at module level (it can outlive a single command),
  so it is now included in the sweep above rather than excused.

## Known deferred items (outside the allowlist, deliberately untouched)

1. Bounded `SET LOCAL lock_timeout` for the dispatch-path
   `UPDATE runs SET dispatched_at=now()` (the statement observed blocked in
   O11) – `dispatch.py` is outside the FAR-1524 allowlists.
2. Fly-side establishment-closure investigation (needs-human, above).
