# ADR 023 — Ongoing-trigger no-delivery auto-deactivation (FAR-188 / FAR-189 / FAR-190)

**Date:** 2026-08-15
**Status:** Accepted

---

## Context

An `ongoing` trigger keeps a pipeline topped up to a target number of in-flight
runs "forever" (FAR-158; PRD §8.5). When the pipeline's work is exhausted — an
empty backlog, a permanently broken source, or a silent infra/sandbox failure
that produces runs but no delivery — the trigger keeps producing no-delivery
runs indefinitely with no operator visibility. A daemon that "silently does
nothing" is worse than one that fails loudly: the pool occupies run slots and
incurring cost while delivering nothing, and nobody is told.

Before FAR-188/189/190, the only auto-deactivation was the FAR-158 **config-
failure guard**: a Redis counter (`_bump_ongoing_failure` in `cron_helpers`)
that deactivates an ongoing trigger after 5 consecutive **CONFIG-level** fire
failures (`no_pipeline` — deleted pipeline, invalid/missing pinned snapshot).
That guard covers dispatch/config breakage but says nothing about **run
outcomes**: an ongoing trigger can fire healthy runs for months that all
complete with no delivery, and no mechanism ever stops it.

This ADR records the architecture for closing that second gap: a DB-backed
no-delivery streak engine that auto-deactivates an ongoing trigger after N
consecutive no-delivery terminal runs, then notifies the operator so they can
investigate the quiet stretch and re-enable when ready.

The feature shipped across three PRs:
- FAR-188 — `runs.raw_output_markers` (JSONB, keyed by attempt_key) so a
  delivery (`pr_url`) can be recovered even when `output.json` fails to parse.
- FAR-189 — `runs.run_classification` (JSONB) — a classifier persisted in the
  SAME transaction as the terminal status write, giving the streak engine a
  durable, queryable outcome record instead of raw status.
- FAR-190 — the streak engine itself (`backend/src/modulo/core/trigger_streak.py`,
  migration `0102_ongoing_streak_epoch`, sweep wired into
  `cron_helpers.dispatcher_reconcile`).

## Decision 1 — Persist run-outcome classification at terminalization (FAR-189)

Every terminal run gets a `run_classification` JSONB record written in the same
transaction as the terminal status write, by a pure classifier keyed on status
(single source of truth in `core/pipeline_engine/classify.py`):

| status | classification |
|---|---|
| `cancelled` / `budget_exceeded` | `excluded` (never countable, breaks the streak walk) |
| `failed` / `eval_failed` / `stalled` | `no_delivery` (**countable** — infra/sandbox crashes elevated to failed count, PO decision) |
| `complete` | `delivered` iff ≥ 1 valid `pr_url`; else countable `no_delivery` (empty-backlog, PO decision) |
| any other terminal | `excluded` fail-safe (a NEW terminal status hits this loudly) |

Reasons are stored on the record (`no_work`, `needs_human`, `source_error`,
`parse_error`, `no_delivery`, `operator_or_hitl_cancelled`, `budget_exceeded`,
`pr_delivered`, `classifier_error`). The write is an upsert, so a
re-terminalization overwrites stale evidence. Raw-SQL terminalizers that bypass
the CRUD hook leave `run_classification = NULL`; the classification reconcile
sweep (`reconcile_missing_classifications`, wired into `dispatcher_reconcile`
every 60s) closes that gap within a minute.

Rationale: the streak engine must not re-derive outcome from raw status or scan
`outputs_json` per tick — the classification is computed ONCE, at the moment the
terminalization facts exist, and persisted for cheap, indexed, queryable
streak walks.

## Decision 2 — DB-backed streak engine run as a sweep, NEVER inline at terminalization

The engine (`trigger_streak.enforce_no_delivery_streaks`) walks an ongoing
trigger's terminal classified runs newest → oldest and counts consecutive
`no_delivery` runs, then deactivates once the count reaches the threshold. It
runs as a system sweep wired into `cron_helpers.dispatcher_reconcile` (every
60s, AFTER the classification reconcile so the newest runs are classified before
the walk reads them) — **never inline in terminalization**.

Streak semantics:

- Countable: terminal runs classified `no_delivery` (including empty-backlog
  completes and infra/sandbox failures elevated to `failed`).
- The walk STOPS at the first `delivered`, `excluded`, or `unclassified` run, and
  at any terminal run with NO classification record at all — **fail-closed**, so
  deactivation can never ride on uncertain evidence.
- Boundary = `GREATEST(last_delivery_at, streak_epoch)`, where
  `last_delivery_at` is `MAX(completed_at)` of runs classified `delivered` and
  `streak_epoch` is the re-anchored activation instant. Runs predating the
  boundary never count.
- Cancelled / budget-exceeded runs are excluded AND break the walk (a
  deliberate human/interrupt stop is not evidence of a quiet stretch).
- The boundary must also be at least `no_delivery_min_window_hours` old before
  the streak can fire (a wall-clock window so other customers' quiet stretches
  never self-deactivate within the first day after a delivery).

Why not inline at terminalization: the FAR-189 lesson — a terminalization-time
hook races the classification write (a fact written after the status write is
invisible to an inline hook at the status write). A sweep that runs after the
classification reconcile observes a consistent classification log. (See
"Alternatives considered and rejected".)

## Decision 3 — The DB streak engine COEXISTS with the FAR-158 Redis config-failure counter

Two mechanisms deactivate the same entity (an ongoing trigger), guarding two
different failure domains:

| | FAR-158 Redis counter | FAR-190 DB streak engine |
|---|---|---|
| Guards | dispatch/config failure (`no_pipeline`: deleted pipeline, invalid/missing pinned snapshot) | run-OUTCOME no-delivery (empty backlog, silent infra crashes, no PRs) |
| Source of truth | Redis (`incr` + 6h TTL), volatile, per-Redis, no history | `runs.run_classification`, durable and queryable (enables UI surfacing + rollback checks) |
| Threshold | `ONGOING_MAX_CONSECUTIVE_FAILURES` = 5 | `max_no_delivery_streak` (default 5) |
| Clear path | `_clear_ongoing_failure` on successful top-up | epoch re-anchored on any `active=True` transition |
| Lifecycle record | `record_ongoing_deactivation_lifecycle` (`deactivated_by='config_failure'`) | same shared helper (`deactivated_by='no_delivery_streak'`) |

Both write identical, searchable audit records (`ongoing_trigger.auto_deactivated`
AuditEvent + fire-outcome TriggerEvent) via the shared
`record_ongoing_deactivation_lifecycle` helper, so an operator investigating a
deactivation sees a single consistent story regardless of which guard fired.

## Decision 4 — Guarded atomic deactivation + notifier + operational guards

**Guarded atomic UPDATE (no TOCTOU).** The deactivation is a single
`UPDATE triggers SET active = false WHERE ... AND active AND <streak count folded
into the WHERE> AND <boundary <= window cutoff> RETURNING ...` — the streak is
computed inside the UPDATE's WHERE from the live trigger row (via a
self-contained scalar-subquery boundary reading `streak_epoch`) and the
classification log. A re-enabled trigger (already `active=false`, or epoch
re-anchored) and a stale sweep tick can never be hit; concurrent ticks produce
one rowcount=1 and the second is a no-op. `RETURNING` carries the streak value so
no second walk is needed for the audit record.

**Known limitation (the deactivation cannot commit on real Postgres yet).** The
deactivation transaction writes a fire-outcome TriggerEvent with
`validation_result='auto_deactivated'` through `cron_helpers._log_ongoing_event`
inside the SAME transaction as the `active=false` UPDATE and the AuditEvent
(see `record_ongoing_deactivation_lifecycle`). But `auto_deactivated` is NOT in
the `ck_trigger_events_validation_result` CHECK-constraint vocabulary —
`VALIDATION_RESULT_VALUES` in `db/models/trigger_event.py` and the hardcoded twin
in migration 0069 — so real Postgres rejects the insert and rolls back the whole
deactivation transaction: the engine can never deactivate anything on real
Postgres today. The mock-based unit tests stay green because they route by
SQL-substring matching and never execute the constraint (AGENTS.md §12). This is
not the intended design — it is the current shipped state. The vocabulary fix
must ship together (add `auto_deactivated` to `VALIDATION_RESULT_VALUES` AND a
NEW migration widening `ck_trigger_events_validation_result`; never edit
migration 0069 in place); once it does, the Consequences below become accurate.

**Notification.** Post-commit, a deactivation dispatches the existing
`EVENT_TRIGGER_DEACTIVATED` notifier event with a sanitised payload
(identifiers + titles + allow-listed reason fields only — never tokens or raw
output). A failed dispatch writes a critical audit entry on the first attempt
and persists a per-org Redis pending marker retried on the next scheduler tick
(bounded: 10s per dispatch, ≤10 inline per tick, ≥15-min per-member retry
cooldown). `delivered_after_deactivation: False` is a contract field — a
delivery landing after deactivation never silently re-activates the trigger.

**Operational guards.**

- **Kill switch** — `MODULO_STREAK_DEACTIVATE_KILL_SWITCH` gates ONLY the
  deactivate+notify side-effect; classification always persists.
- **Per-org per-hour cap** — `ONGOING_STREAK_DEACTIVATE_MAX_PER_ORG_PER_HOUR`
  (10): a burst is deferred to the next tick rather than cascading.
- **Mass-cascade alert** — when an org deactivates ≥ 5 triggers in 24h (an
  infra-outage signature), a critical alert fires once per window, deduplicated
  via the DB audit chain (never Redis, so a Redis outage degrades to a
  duplicate alert, never a suppressed one).

## Decision 5 — Config surface and streak boundary anchoring

Per-trigger `config_json`:

- `max_no_delivery_streak` — the threshold (default 5), with a one-release
  fallback to the legacy `max_consecutive_failures` key. Only genuine `int`
  values are accepted; a mis-typed config falls back to the default (never fires
  instantly).
- `no_delivery_min_window_hours` — the wall-clock window (default 24h product).
  The dogfood deployment overrides the default to 0 via
  `MODULO_ONGOING_STREAK_MIN_WINDOW_HOURS` so a fast-moving repo's quiet stretch
  still stops the pool.

`streak_epoch` (migration `0102_ongoing_streak_epoch`, nullable timestamptz,
server default `CURRENT_TIMESTAMP`):

- Backfilled at deploy to the migration instant — pre-existing no-delivery
  history can never mass-deactivate on tick 1 because every old run's
  `completed_at` precedes the anchored epoch.
- Re-anchored on every `active=True` transition (create / update / toggle /
  restore / re-enable, plus the cost-controller circuit-breaker reset) via the
  shared `anchor_trigger_streak_epoch` helper, inside the write transaction so
  the epoch commits atomically with the flip. A re-enabled trigger's streak
  restarts from its re-enable moment.
- A NULL epoch (rolling-deploy skew) COALESCEs to `now()` → the boundary becomes
  "now", no run counts, and the trigger can never deactivate until re-anchored
  (fail-safe).
- Migration `0102` also adds `ix_runs_streak_engine` — a NON-partial
  `(trigger_id, completed_at DESC)` index serving the 60s sweep's correlated
  subqueries (the old partial `run_classification IS NOT NULL` predicate could
  never be implied by the engine's `->> 'value'` filters).

Re-enable flow: when the operator re-activates the trigger, the epoch is
re-anchored AND the FAR-158 Redis config-failure counter is cleared
(`clear_trigger_streak_after_reenable`, post-commit, best-effort) so a stale
counter never re-deactivates it on the next failure.

## Alternatives considered and rejected

**(a) Redis counter only (extend FAR-158).** Rejected — the config-failure
counter is volatile (per-Redis, 6h TTL, no history), unsuitable for a
run-outcome guard that must be queryable (UI surfacing, rollback checks) and
survive a Redis flush.

**(b) Inline check at terminalization.** Rejected — a terminalization-time hook
races the classification write: the FAR-189 lesson is that a fact written after
the status write is invisible to an inline hook at the status write. The sweep
runs after the classification reconcile and observes a consistent log.

**(c) Ad-hoc `outputs_json` scan at streak-query time.** Rejected — re-deriving
outcome from raw node output on every sweep tick is expensive (wide-row scans
every minute) and leaves no persisted record. Persisted classification (Decision
1) makes the walk a cheap indexed query over `run_classification`.

## Consequences

- An ongoing trigger that produces N consecutive no-delivery terminal runs is
  auto-deactivated (default N=5) and the operator is notified — a silently-dry
  daemon no longer runs forever. The operator re-enables after investigating;
  re-enable restarts the streak from zero (epoch re-anchored).
  **Known limitation:** this consequence does NOT hold on real Postgres today —
  the deactivation transaction is rejected by `ck_trigger_events_validation_result`
  until `auto_deactivated` is added to `VALIDATION_RESULT_VALUES` and a new
  migration widens the constraint (AGENTS.md §12). It becomes accurate once that
  vocabulary fix ships.
- Two auto-deactivation mechanisms now exist for ongoing triggers (config
  failure via Redis, no-delivery via DB). They guard different failure domains,
  deactivate the same entity, and share one audit ceremony — new guards must
  route through `record_ongoing_deactivation_lifecycle` to keep the audit story
  consistent.
- Deactivation is eventually consistent with a bounded lag: the sweep runs every
  60s, so there is up to ~1 minute between the threshold being crossed and the
  `active=false` flip (plus the classification reconcile before it).
- The notification pipeline is best-effort and can never break the sweep tick:
  a hung endpoint is bounded (10s), failures are retried from a Redis pending
  set, and the deactivation (audit + trigger_events) is the durable truth.
- The guards protect against cascades (per-org cap), outage blindness
  (mass-cascade alert), and emergency disable (kill switch).
- `streak_epoch` is a new nullable column on `triggers`; any new active-write
  site MUST re-anchor it through `anchor_trigger_streak_epoch` (there is no
  un-anchored activation path).
