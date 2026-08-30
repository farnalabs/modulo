# ADR 026 — Extract `node_telemetry_json` from `runs` (write cut-over, merged reads, retention-drain gate)

**Date:** 2026-08-28
**Status:** Accepted (FAR-470)

---

## Context

`runs` is the hottest table in the product: millions of rows, multi-MB TOASTed
values, and **four jsonb blobs** — `outputs_json` (rewritten on every node
completion), `node_telemetry_json` (a mutable accumulator rewritten on every
node completion), `node_token_usage`, and `cost_breakdown`. The 2026-08-27/28
deploy outage was caused by a json→jsonb conversion on this table: the backfill
ran for hours under an `AccessExclusiveLock` on `runs`, was killed mid-flight
by repeated deploy attempts, and turned a routine schema change into a
multi-day outage. The root cause is structural, not accidental: per-node
telemetry does not belong in a wide, hot, row-purged table.

Consumers of `node_telemetry_json` today:

- the **fenced finalize merge** (split_node_output idempotence) — a raw-SQL
  write that updates `outputs_json` and `node_telemetry_json` atomically;
- the **node-output API** (`GET /runs/{id}/nodes/{nid}/output`) — its masking
  path `_normalize_node_telemetry` unions node ids from outputs AND telemetry;
- the **recovery guard** — "node_id in outputs OR telemetry ⇒
  NodeAlreadyCompletedError"; recovery skip markers are written into the blob
  by `core/pipeline_engine/recovery.py`, a write site OUTSIDE the fenced SQL;
- **cost finalize**, **analytics daily-facts SQL** (ORM Core SQL in
  `core/analytics/maintenance.py` under `set_rls_user_context`), **frontend run
  views** (via API), **run retention** (serialize + `purge_terminal_runs`;
  `batch_delete_old_terminal_runs` is a separate purge path to inventory), and
  **org deletion**.

There are **FOUR telemetry write sites**: the fenced SQL, the ORM
`update_run_status`, an update helper, and `recovery.py`'s direct ORM mutation.

House patterns this design builds on: OrgScoped models (UUID PK +
`organisation_id` + TimestampMixin) with RLS (ENABLE+FORCE,
`rls_org_isolation`, `enforce_same_organisation` tenant triggers); per-node
current-state upsert tables (`run_evidence`, `node_observations` —
UNIQUE(run_id, node_id), DO UPDATE upserts); bounded idempotent anti-join
sweeps with OTel gauges; SystemConfig flat snake_case runtime keys; testcontainers
integration tests; custom semgrep rules; generic `sa.JSON` in the ORM (JSONB in
migrations); advisory locks; `_json_bind` + `CAST(:payload AS jsonb)` for dict
binding in raw SQL (see ADR 020 for the facts-table sibling of this campaign).

The decision was hardened through **four iterations of 7-lens adversarial
review** (trail on Linear FAR-470); this document is the consolidated outcome
and is self-contained.

## Decision 1 — Scope: extract `node_telemetry_json` only; freeze the other three columns

Only `node_telemetry_json` is extracted in this campaign. `outputs_json`,
`node_token_usage`, and `cost_breakdown` become **FROZEN COLUMNS**: a semgrep
rule over `db/migrations/versions/*.py` rejects `ALTER TABLE runs` against
them (allowlist mechanism documented; if the Phase 0 measurement selects the
telemetry+outputs campaign, the freeze list shrinks before the rule lands).

Honest scope note: `outputs_json` write churn persists (declared residual); a
follow-up decision to also extract it is gated on the Phase 0 measurement. The
fenced finalize write is touched in Release A (telemetry write moves out of the
legacy branch's successor) and Release B (dead legacy branch removed).

## Decision 2 — Target design: a per-node current-state table

New table **`run_node_telemetry`** — a per-node CURRENT-STATE table following
the house idiom (`run_evidence`, `node_observations`):

- OrgScoped: UUID PK, `organisation_id` FK to `organisations`, TimestampMixin.
  `organisation_id` is **derived server-side from the `runs` row**, never from
  client input.
- **UNIQUE(run_id, node_id)** — one row per node, upserted.
- `payload`: generic `sa.JSON` in the ORM / JSONB in the migration — a
  **SNAPSHOT** (self-contained per-node state, not deltas); the snapshot
  contract is documented on the model. `payload` is **NOT NULL** (empty object
  for empty state).
- FK to `runs` **ON DELETE CASCADE** — retention purges cascade telemetry with
  their runs.
- RLS ENABLE+FORCE + `rls_org_isolation` + the two
  `enforce_same_organisation` tenant triggers.
- `updated_at` maintained on every write (the recency guard in Decision 7
  depends on it).

NULL/`{}`/missing-key/null-valued-entry mapping is tabulated here and covered
in the parity matrix:

| blob state | table representation | read semantics |
|---|---|---|
| `node_telemetry_json = NULL` / `{}` | no row | node has no telemetry |
| entry missing for a node_id | no row for that node_id | node has no telemetry |
| entry present, value `null` | row with `payload = {}` | empty state (never a null payload) |
| entry present, object | row with `payload = <object>` | the state |

## Decision 3 — ONE write seam with per-kind conflict semantics

All four existing write sites route through **one write seam**: a Python
function `write_node_telemetry(...)` for the three ORM sites (`update_run_status`,
the update helper, `recovery.py`) plus a shared SQL fragment/binding helper in
`db/crud/run.py` (using `_json_bind` + `CAST(:payload AS jsonb)`) for the
fenced raw-SQL write. Semgrep-enforced from Release A: no telemetry column
literal outside the seam.

**Per-kind conflict semantics** at the seam (deliberate: completion writes are
once-per-node fences; current-state writes are last-write-wins):

| write kind | examples | ON CONFLICT (run_id, node_id) | failure mode |
|---|---|---|---|
| **completion-class** | node completion finalize; recovery skip marker | `DO NOTHING` + rowcount check raising the conflict | second completion raises `NodeAlreadyCompletedError` — the DB-level fence behind split_node_output idempotence is preserved |
| **non-completion (current-state)** | started/status-class telemetry | `DO UPDATE SET payload, updated_at` | last-write-wins — the house current-state upsert idiom |
| **system repair sweep** | backfill / re-sync / repair (Decision 7) | `DO UPDATE` with a **recency guard** (update only if the blob entry is newer than the table row) | the table never regresses to an older blob state |

The completion-class arm is a **PLAIN `INSERT ... VALUES`** (no filtering
source), so an RLS misconfiguration raises `42501` loudly instead of
masquerading as a completion conflict. Invariant: **"rowcount 0 ⇒ arbiter
conflict, provably the only zero-row path."** This is a documented deliberate
deviation from the `run_evidence` DO UPDATE house upsert (rationale:
completion-class once-per-node fence).

The failure-marker exclusion rule (raw-output markers never appear in
telemetry) is restated for the new table and enforced at the seam: a
payload-shape guard inside `write_node_telemetry` plus a semgrep assertion.

## Decision 4 — Release A finalize atomicity: ONE transaction

In `cut_over`, the fenced finalize is **one database transaction — ideally one
CTE statement** — performing the `outputs_json` UPDATE and the
`run_node_telemetry` upsert together. A crash between the two effects is
impossible by construction. A **fault-injection test** aborts between the two
statements and asserts all-or-nothing (either both land or neither does).

Fenced-SQL shape per state:

| state | fenced finalize shape |
|---|---|
| `legacy` | unchanged legacy behaviour: UPDATE `outputs_json` + rewrite `node_telemetry_json` |
| `cut_over` | single CTE: UPDATE `outputs_json` + upsert `run_node_telemetry` (completion-class DO NOTHING + rowcount fence) |

## Decision 5 — State machine: TWO runtime states only

One SystemConfig key **`node_telemetry_migration_state`** (flat snake_case)
with exactly two runtime values:

| state | meaning | finalize writes to |
|---|---|---|
| `legacy` | blob is the write store (deploy-time default for existing fleets; also the post-rollback state) | `node_telemetry_json` |
| `cut_over` | table is the write store; blob is frozen read-only legacy | `run_node_telemetry` only |

`contracted` is **NOT a runtime state** — it is the code-delivery milestone
"Release B" (reference removal), reached only after the retention-drain gate
(Decision 10).

Legal transitions (enumerated; anything else is rejected at the config
writer):

| from | to | precondition |
|---|---|---|
| `legacy` | `cut_over` | cutover runbook gates (Decision 8) |
| `cut_over` | `legacy` | write-rollback (Decision 9) |
| same state | same state | no-op |

**Absent-key default is sentinel-dependent:** before the first cutover the
absent key resolves to `legacy`; once a **first-cutover sentinel** exists, an
absent key resolves to `cut_over` (fail-safe: writes stay on the table). The
sentinel is a second SystemConfig key, `node_telemetry_cutover_sentinel`,
written by the runbook at the first successful flip and carrying the repair
sweep's completion sentinel required before a post-rollback re-cutover is
permitted (Decision 9). **Both keys are on the housekeeping exclusion list**
(protected from config-cleanup sweeps).

**Fresh-install posture:** the Release A migration seeds the state key to
`cut_over` on fresh schemas (empty blob — no legacy data), so new installs
never inherit the campaign; existing fleets default `legacy` until the runbook
flip.

## Decision 6 — Merged reads ALWAYS ON from Release A, with state-aware precedence

From Release A (not at cutover), every read goes through one accessor
implementing **merged reads at per-node grain — never additive**:

| state | per-node read precedence |
|---|---|
| `cut_over` | table row if present, else parsed legacy-blob entry |
| `legacy` (incl. post-rollback) | parsed legacy-blob entry if present, else table row |

Because the merge grain is per-node, in-flight runs at cutover are seamless
(nodes already in the table read from it; nodes still only in the blob fall
through). The per-run "blob drained" optimization is deferred (YAGNI); read
amplification on legacy nodes is accepted and documented (legacy runs age out
via retention).

Masking **reuses `_normalize_node_telemetry`** over the accessor output; a
differential test runs the blob-path vs the row-path over a production sample.
Frontend API shapes are unchanged.

**Analytics:** NO SQL view. A single state-aware Core-SQL expression helper in
`db/crud/run.py` (house location) implements the merged per-node grain for the
daily-facts queries; daily-facts switches at Release A. The helper encapsulates
the precedence so no daily-facts query branches on state outside it.

## Decision 7 — ONE universal repair sweep (recency-guarded DO UPDATE)

A **single sweep** serves backfill, post-rollback repair, AND re-sync — there
is no separate backfill and no separate re-sync implementation. It is a
stateless, bounded, per-org sweep:

- **Candidate selection**: anti-join — blob entries absent from the table, or
  present but older than their blob entry. In **steady state** this anti-join
  is the driver. During **rollback windows** the temporary audit trigger's log
  (Decision: Phase 0c) drives selection — runs with any blob telemetry write in
  [rollback, re-cutover] are selected exhaustively (not sampled).
- **Write**: `INSERT ... ON CONFLICT (run_id, node_id) DO UPDATE` where the
  update is **recency-guarded** — insert if absent; update only if the blob
  entry is newer than the table row (`updated_at` vs the per-entry ordering
  key pinned by the Phase 0 census; an entry lacking an ordering key inserts
  absent rows but never updates an existing one — conservative no-regress).
- **Verbatim copy, presence-only**: entries are copied without completion-class
  filtering — identical posture to the recovery guard's blob arm (Decision 8),
  so guard/sweep parity holds **by construction**.
- **Duplicate node_ids**: latest-wins per the Phase-0-pinned ordering key
  (`DISTINCT ON`).
- **Batches**: BYTE-based capped batches with a ≥1-row-per-batch guarantee (a
  single oversized payload gets its own batch — no skip-oversized infinite
  loop); max-size/TOAST-boundary payload is in the test matrix. Remainder is
  re-enqueued until a **zero-write pass** (a pass that inserts and updates zero
  rows) — the termination criterion.
- **Scheduling cursor**: none — the house stateless idiom is preferred; each
  pass re-scans via the anti-join and the zero-write pass terminates it.
- **Tenancy**: per-org iteration via `set_rls_org` (FORCE RLS
  silent-zero-rows trap). Negative test: the sweep without org context matches
  zero rows LOUDLY.
- **Malformed rows**: quarantine + counter (documented accepted-loss or
  repaired before the drain gate).
- **Operations**: OTel gauges + stall alarm + an **external liveness check**
  independent of the sweep (heartbeat metric; alert on "candidates > 0 AND no
  heartbeat for N minutes" — a sweep that never runs is invisible to its own
  alarms). The sweep keeps running (throttled) until the retention-drain gate
  passes — it is not a one-shot.

## Decision 8 — Recovery guard: presence-only blob arm

The recovery guard ALWAYS unions ALL THREE ARMS regardless of state:
`node_id in outputs OR row in run_node_telemetry OR entry in legacy blob` —
and the blob arm is **PRESENCE-ONLY**: ANY blob entry for the node_id trips,
with no completion-class filtering.

Rationale: this preserves today's behaviour exactly — **zero semantic change
during the migration**. The known started-only false-trip is documented as
pre-existing behaviour; its fix is a separate small follow-up AFTER Release B
(once the blob arm is dead code, the false-trip dies with it). The repair
sweep is a verbatim presence-only copy (Decision 7), so guard/sweep parity
holds by construction — no classifier exists on either side.

Expected outcomes (state × arm × guard result):

| arm holding node_id | `legacy` guard | `cut_over` guard | post-Release-B guard |
|---|---|---|---|
| outputs only | trips | trips | trips |
| table row only | trips | trips | trips |
| blob entry only (completion-class or started-only) | trips | trips | arm removed — trips iff outputs or table row |
| started-only blob entry (pre-existing false-trip) | trips (documented pre-existing behaviour; unchanged) | trips | dies with the arm (follow-up) |

Malformed blob entries: the guard **skips the entry and increments a counter**
(fail-closed is rejected — it would wedge runs), a documented deliberate
divergence from the sweep's quarantine. Guard parity is tested over every
seeded data state (Decision 12).

## Decision 8b — Cutover flip: operational gates, never in-release

The `legacy → cut_over` flip is an OPERATIONAL runbook step, gated on:

1. **Continuous fleet lease/heartbeat** — every pod class (API pods, SAQ
   workers, schedulers — enumerated) reporting Release A with a lease renewed
   during the flip, not a point-in-time version scrape. A post-gate stale
   heartbeat is an auto-rollback condition.
2. **State-visibility probe** — read the key back through the access path the
   runtime uses, not just the config row.
3. **Canary `/runs` read** — asserting telemetry identical pre/post flip,
   including one legacy-blob-only node and one table-row node.

Post-cutover, a **blob-write tripwire** (metric + alert) guards against stray
blob writes; a finalize transaction started under `legacy` committing just
after the flip is expected noise (tolerance window in the runbook).

## Decision 9 — Rollback and re-cutover

**Write-rollback (`cut_over → legacy`)**: writes revert to the blob; reads
become blob-precedence (Decision 6); the universal repair sweep — driven by the
audit-trigger log for the window — repairs table rows; the sweep writes its
completion sentinel into `node_telemetry_cutover_sentinel`; re-cutover is
**refused** by the config writer without that sentinel (negative test
implementable). The bounded-staleness window (blob-wins reads serve fresh
blob; stale table rows shadowed until repaired) is a documented property of
this state.

**Rollback after Release B**: image-revert FIRST, state-flip second, with the
same fleet-gate discipline (sequence-tested). Release B is code-revertable (a
revert restores the fallback arms; the column exists).

## Decision 10 — Retention, the retention-drain gate, and the descoped column drop

**Retention registration**: `run_node_telemetry` is registered in the
`purge_terminal_runs` FK-CASCADE docstring list; `batch_delete_old_terminal_runs`
is inventoried in Phase 0 with explicit disposition (cascade-covered vs needs
update). NO bespoke purge sweep, NO orphan scan, NO `created_at` retention
index unless a concrete requirement emerges. Retention size-accounting is
updated for the new table.

**Retention-drain gate** (gates Release B — reference removal):

```sql
SELECT count(*) FROM runs
WHERE node_telemetry_json IS NOT NULL AND node_telemetry_json <> '{}';
-- must be 0, AND the backfill quarantine must be accounted
-- (quarantined entries documented as accepted-loss or repaired)
```

Until the gate passes, the system **PARKS in `cut_over`** — a supported
indefinite configuration (merged reads correct; blob frozen; dwell-age alert;
the standing cost of the retained column documented in this decision). There
is no deadline pressure to drain.

**The physical column drop is DESCOPED INDEFINITELY.** `node_telemetry_json`
is documented deprecated in the self-hosted schema changelog (external BI
readers are out of gates' reach), is purged whole with its runs by the
existing retention purge, and a **sibling semgrep rule rejects
`DROP COLUMN` / `ALTER` on the deprecated column outside an ADR-allowlisted
migration id** — machine-enforced protection of the rollback/repair source.
A future drop re-opens this ADR with a new migration id.

## Decision 11 — Release sequence

- **Release A (code + table)**: migration creates the (empty) table
  (`lock_timeout` + retry on the FK DDL as house discipline); accessor + write
  seam land; merged reads ON with `legacy` precedence; semgrep bans (telemetry
  column literal outside the seam; frozen columns; deprecated-column drop
  guard) are Release A gates. State stays `legacy` at deploy time. Down
  migration is **DEFENSIVE**: raises if state ≠ `legacy` or the table is
  non-empty (negative migration test); SystemConfig keys are deleted by the
  runbook, never written by Alembic.
- **Cutover flip**: operational (Decision 8b).
- **Drain period**: universal sweep runs throttled; retention purges
  blob-bearing runs; system may park in `cut_over` indefinitely (Decision 10).
- **Release B (reference removal, AFTER the drain gate)**: ORM attribute,
  fenced-SQL legacy branch, accessor blob-fallback arm, guard blob arm,
  analytics legacy branch — all removed atomically (the arms are provably dead
  code once the gate passes). A guard-delta test covers the arm removal
  (post-B behaviour for started-only entries: never trips — uniform with the
  removed arm). The column is NOT dropped (Decision 10).

## Phase 0 — Measurement + facts gate (before implementation)

1. **Blob-size histograms + per-column write-churn measurement** from prod —
   decides telemetry-only vs telemetry+outputs for this campaign, and
   pre-commits the numeric rule: **"telemetry ≥ X% of measured churn ⇒
   telemetry-only viable; otherwise both"** — so acceptance criterion (6)
   cannot fail after the campaign completes.
2. **Empirical blob-content census**: do legacy blobs contain started-only
   (non-completion) entries? Does each entry carry a completion timestamp (the
   ordering key for latest-wins and the recency guard)? What is the structural
   shape (object keyed by node_id vs array)? These facts pin the sweep's
   ordering key and the duplicate-winner rule against real data.
3. **Access-pattern audit** enumerating every reader/writer INCLUDING raw SQL
   (fenced finalize, retention serialize + BOTH purge paths, analytics
   daily-facts, node-output API masking, recovery guard, org deletion, cost
   finalize, run-view serialization).
4. **Temporary audit trigger** (`UPDATE OF node_telemetry_json`): a **LOGGED**
   table (evidence must survive crashes) maintained as a **ring buffer with a
   cap-exhaustion alarm**, created with `lock_timeout` in a quiet window; the
   drop step is owned by Release B. During rollback windows its log exhaustively
   selects repair-sweep targets (Decision 7). A **synthetic gate** in rehearsal
   fires one recovery-marker write and asserts the trigger observed it; **a
   trigger hit blocks the cutover flip**.

## Testing matrix

Per-PR integration scope is PINNED: `ci.yml` gains a bounded testcontainers
job (migration up/down, matrix, guard parity, concurrency, kill-and-resume)
with a stated budget of **20 CI minutes** wall-clock; load/bloat/autovacuum
runs nightly.

1. **Data-state matrix**: legacy-blob-only / table-only / mixed per-node /
   both-stores-equal / both-stores-differing / empty × write state
   (`legacy` / `cut_over`) — merged reads and guard over every cell.
2. **Payload distinctions**: NULL vs `{}` vs missing-key vs null-valued-entry
   (Decision 2 table) + **malformed-legacy** entries (guard skip + counter;
   sweep quarantine).
3. **Guard parity** incl. the started-only expected-outcome table (Decision 8).
4. **Concurrent-finalize**: completion-class DO NOTHING + rowcount raise —
   exactly one success, one conflict.
5. **Crash fault injection**: abort between the finalize's two effects;
   assert all-or-nothing (Decision 4).
6. **Skip-then-finalize sequencing** (recovery marker then completion attempt).
7. **Sweep × finalize interleaving** (two sessions) + **kill-and-resume** of
   the sweep validating the zero-write-pass criterion.
8. **Rollback-and-re-cutover FULL sequence**: cutover → write → rollback →
   write → repair sweep → re-cutover → read, per-node correctness at every
   step; re-sync during an incomplete backfill; re-sync encountering a newer
   table row (recency guard holds).
9. **Re-cutover without the completion sentinel rejected**; illegal
   transitions rejected at the config writer.
10. **Purge-vs-fallback sequence** (cascade purge then legacy-only read).
11. **Masking differential**: blob-path vs row-path over a production sample.
12. **Retention serialization parity** (legacy serialization vs accessor
    serialization of the same run).
13. **Analytics daily-facts parity**: merged-grain helper == legacy
    computation over the seeded matrix (duplicates, malformed, mixed) + prod
    shadow-compare sample.
14. **Write-health signals**: rowcount-0-WITHOUT-exception rate (the RLS
    silent-zero mode) as the write-rollback signal — raised conflicts counted
    separately as fence events, never triggering rollback; table-write
    tripwire while state = `legacy`.
15. **Migration up/down** incl. the defensive-raise negative test; **fresh
    install** (migrate up on empty DB ⇒ state seeded `cut_over` ⇒ finalize
    lands in the table); **audit-trigger capture** test.

## Prod-scale rehearsal

Before the prod cutover: migration + repair sweep + read path end-to-end
against a prod-scale dataset (redacted snapshot validated post-redaction:
pre/post length histograms + malformed census must match; env pinned to prod
PG major + autovacuum settings + prod-equivalent instance class); the
migration runs under **REPLAYED production-shaped concurrent write load** (the
outage shape was lock contention under live traffic) with `lock_timeout`
behaviour observed; at least one mid-rehearsal sweep kill+resume at prod scale
validating the stall alarm + zero-write-pass criterion; batch timing tunes the
byte throttle; read-only parity reconciliation against prod data; **finalize
p95 baseline captured from PRODUCTION**.

## Acceptance criteria

1. Universal repair sweep achieves a zero-write pass across the fleet.
2. Shadow-compare diff = 0 over ≥ 7 days including a weekend.
3. **Retention-drain gate passes**: zero blob-bearing runs under retention +
   quarantine accounted (Decision 10).
4. Finalize p95 within +10% of the production-captured baseline.
5. Storage delta within ±20% of the rehearsal-predicted band.
6. Steady-state `runs` **dead-tuple ratio ≤ 0.6× the Phase-0 baseline** AND
   **TOAST growth ≤ 0.7× baseline** (numeric, pinned — the problem metric).
7. Release B reference removal complete, static check clean, suite green.
8. Documentation set delivered: this ADR (incl. per-consumer disposition,
   state/transition, and config knob tables), ops runbook, operator config
   doc, self-hosted schema changelog entry (deprecation), TESTING.md
   inventory.

**Per-consumer disposition:**

| consumer | Release A | Release B |
|---|---|---|
| fenced finalize write | state-shaped (Decision 4 table) | legacy branch removed |
| ORM write sites (×3) | routed through the seam | unchanged |
| node-output API masking | accessor merged read + `_normalize_node_telemetry` | blob arm removed |
| recovery guard | three presence-only arms (Decision 8) | outputs + table row only |
| cost finalize | accessor merged read | table read |
| analytics daily-facts | merged-grain helper | table-only expression |
| frontend run views | unchanged API shapes | unchanged |
| retention serialize | accessor read | table read; column deprecated |
| purge paths | FK cascade registered; batch path dispositioned per Phase 0 | unchanged |

## Observability

Pinned thresholds:

- **Sweep**: rows/sec + ETA + lag; **stall alarm** (candidates > 0 AND no
  heartbeat for N minutes); external liveness check (Decision 7).
- **Write-health**: rowcount-0-without-exception rate (rollback signal);
  raised conflicts counted separately as fence events.
- **Tripwires**: post-cutover blob-write tripwire (tolerance window);
  table-write tripwire while state = `legacy`.
- **State**: alarm on ANY transition regardless of direction; state-heartbeat
  ("state has been X for N days") distinguishing silent revert from deliberate
  park; **dwell-age alert** for the `cut_over` park.
- **Guard regression**: `NodeAlreadyCompletedError` 7-day baseline, >20% for
  1h ⇒ halt flip / investigate (dashboard + runbook trigger, not a new pager).
- **Reconciliation/shadow mismatch count** (thresholds pinned; automated
  read-rollback vs pager-driven stated per signal); quarantine + guard
  fail-closed counters; **audit ring-buffer cap-exhaustion alarm**.
- Per-phase structured logs; ops runbook; dashboard.

## Config knobs

| knob | mechanism | default | authority |
|---|---|---|---|
| `node_telemetry_migration_state` | SystemConfig key | `legacy` pre-first-cutover; absent ⇒ `cut_over` once the sentinel exists | config writer validates transitions (Decision 5) |
| `node_telemetry_cutover_sentinel` | SystemConfig key (first-cutover marker + repair-sweep completion sentinel) | absent until first flip | cutover runbook / repair sweep |
| housekeeping exclusion | both keys on the exclusion list | excluded | housekeeping config |
| dwell-age alert threshold | alert rule | pinned at rollout | observability config |
| sweep byte-batch cap + throttle | sweep constant | tuned by rehearsal | code constant |
| CI job budget | ci.yml job timeout | 20 minutes | code constant |

## Alternatives considered and rejected

- **Big-bang column drop during the migration** — this is the 2026-08-27/28
  outage shape (hours-long backfill under `AccessExclusiveLock`, killed
  mid-flight by deploys). Rejected; the drain gate + park posture (Decision 10)
  replaces it.
- **Completion-class filtering in the guard's blob arm** — would change guard
  semantics mid-migration (started-only entries stop tripping while the arm is
  live), splitting behaviour across states and needing a classifier in the
  sweep for parity. Rejected in favour of presence-only parity (Decision 8);
  the false-trip fix follows Release B.
- **Separate anti-join backfill + separate DO UPDATE re-sync implementations** —
  two code paths to keep semantically identical under rollback windows.
  Rejected in favour of one universal recency-guarded sweep (Decision 7).
- **SQL view for analytics** — a view bakes legacy-branch SQL into the schema
  and complicates Release B. Rejected in favour of a Core-SQL helper
  (Decision 6).
- **Dropping the column at Release B** — external BI readers read the
  self-hosted schema directly; a drop breaks them and destroys the
  rollback/repair source. Rejected; drop descoped indefinitely behind a semgrep
  guard (Decision 10).

## Consequences

- `runs` stops accumulating per-node telemetry churn: dead-tuple and TOAST
  growth fall to the pinned acceptance thresholds (criterion 6), and the class
  of outage that motivated this ADR cannot recur for this column.
- Completion-class idempotence is enforced by a DB-level fence on the new
  table (identical strength to today's blob fence); current-state writes use
  the house upsert idiom; both are centrally reviewable in one seam.
- Merged reads from Release A mean the cutover flip is invisible to readers;
  rollback is a state flip plus a bounded repair window, never a data loss
  event.
- The system may park in `cut_over` indefinitely without deadline pressure;
  the standing cost is the retained (frozen, deprecated) column until
  retention purges its runs.
- Two SystemConfig keys participate in runtime behaviour; both are protected
  from housekeeping cleanup, and the config writer is the single authority for
  transitions.
- Semgrep now guards three things: telemetry writes outside the seam, ALTERs
  on the three frozen columns, and any ALTER/DROP on the deprecated column
  outside an allowlisted migration id.
- The started-only guard false-trip persists until Release B + follow-up —
  documented, unchanged, and scheduled.

## Process and references

- Linear **FAR-470** — ticket and the full review trail: **four iterations of
  7-lens adversarial review** produced the presence-only guard arm, per-kind
  conflict semantics, the single universal repair sweep, finalize
  transactional atomicity, the retention-drain gate + indefinite column-drop
  descope, and the two-state machine.
- 2026-08-27/28 deploy outage — json→jsonb conversion on `runs` (motivation).
- ADR 020 — `run_daily_facts` (the daily-facts consumer and house facts-table
  pattern this design composes with).
- House idioms referenced: OrgScoped + RLS + tenant triggers; current-state
  upsert tables; bounded stateless sweeps; SystemConfig flat keys; semgrep
  custom rules.
