# ADR 028: Configurable run-level retry backoff (`backoff_schedule`)

- **Status:** Accepted
- **Ticket:** FAR-525 (revisits FAR-136)
- **Date:** 2026-09-01

## Context

FAR-136 gave run-level `retry_policy` re-dispatch a hardcoded, jittered
exponential backoff: base 45s, ×2 per attempt, capped at 300s, uniform +25%
jitter, before the SAQ job re-raises `RunRetryPolicyError` and SAQ re-enqueues
after `settings.saq_retry_delay`. The hardcoded 45s floor is too slow for
fast-failing pipelines (eval-guardrail loops where an immediate re-attempt is
the point) and the ×2 growth is wrong for rate-limit recoveries that need a
longer, fixed wait. Operators had no way to tune either.

## Decision

Add an OPTIONAL `backoff_schedule: {delay_seconds, multiplier?}` key to the
RUN-LEVEL `retry_policy` only:

```json
{
  "on": ["stall", "timeout", "failure", "eval_failed"],
  "max_retries": 0-5,
  "backoff_schedule": { "delay_seconds": 45, "multiplier": 2.0 }
}
```

- **Shape & bounds.** `delay_seconds` is an integer in [1, 300] (integral
  floats like `300.0` accepted; bools, NaN, Infinity, non-integral floats
  rejected). `multiplier` is a number in [1.0, 10.0] (bools rejected; ints
  coerced to float). `null`, `{}`, and absent all mean "no schedule".
  Unknown keys INSIDE `backoff_schedule` are rejected at the write sites.
  A non-empty object missing `delay_seconds` is malformed.
- **Effective in-job sleep** = `min(delay_seconds × multiplier^(attempt-1), 300)`
  plus the hardcoded +25% proportional jitter, total clamped to 300. The 300s
  cap is a CODE-HELD pair of constants, not user-configurable, with DISTINCT
  roles (FAR-525 qa gate attribution): `retry_compensation.RETRY_SCHEDULE_MAX_DELAY_SECONDS`
  is the INPUT-VALIDATION bound for `delay_seconds` (what the write-site
  validator and the runtime resolver accept), while the executor's
  `_RETRY_BACKOFF_CAP_SECONDS` (the `cap` default of `_retry_backoff_seconds`)
  is the actual SLEEP clamp applied to the computed delay. Both are 300 today;
  raising one without the other silently diverges accepted input from
  effective behaviour (an accepted 600s input would still sleep at the old
  clamp, or an accepted-at-300 input would clamp a lower bound — either way
  the operator-facing contract and the runtime disagree). The jitter stays
  hardcoded.
- **`multiplier` defaults to 2.0** — "present with defaults" is behaviourally
  identical to "absent" (`{delay_seconds: 45}` ≡ today's behaviour). This
  matches the sibling backoff layers (node retries, edge retries) and lets a
  future base-delay change propagate to schedule-configured pipelines.
  `multiplier: 1.0` is a fixed delay (no growth).
- **Legacy `backoff` key untouched.** The numeric `backoff` keeps its exact
  current semantics: node-default inheritance via `_policy_from_pipeline_default`
  (node retries inherit ONLY this value; default 0 = zero sleep).
  `backoff_schedule` does NOT pace node-level retries and `_policy_from_pipeline_default`
  never reads it. Compounding: a node retry sleeps `backoff` seconds inside
  the run, and the run-level schedule sleeps between whole-run re-dispatches
  — the two multiply into the wall-clock worst case, they do not add.
- **Runtime resolver is TOTAL fail-open (all-or-nothing).** The pure resolver
  `retry_compensation.resolve_backoff_schedule` re-checks bounds before
  computing; ANY structural fault (unknown key, out-of-bounds, wrong type,
  NaN/Infinity, missing `delay_seconds`) → the hardcoded default schedule
  (45s × 2.0, cap 300, jitter) for the WHOLE schedule, never a partial
  application, plus a structured warning log
  (`pipeline.retry_policy_schedule_fail_open`) carrying `run_id`, `reason`,
  and a BOUNDED/REDACTED offending-value snippet
  (`sanitize_retry_policy_snippet`: scalar-only rendering, token-like keys
  redacted, truncated). Fail-open rather than fail-closed because the sleep
  is pacing, not correctness — a defensive default can only deviate toward
  the fleet-proven hardcoded schedule.
- **Per-attempt re-resolution.** Each retry is a fresh SAQ job that re-reads
  the CURRENT pipeline row and re-resolves the schedule — a mid-run PATCH to
  `backoff_schedule` takes effect on the NEXT attempt.
- **Validation layering: strict at write, warn+fail-open at runtime.**
  - `GraphValidator.check_retry_policy` (core `on`/`max_retries`) is
    UNCHANGED: hard error at the write sites AND at run start
    (`_load_execution_context` raises `GraphValidationError`).
  - NEW `GraphValidator.check_retry_policy_schedule` emits a DISTINCT issue
    code `RETRY_POLICY_SCHEDULE_MALFORMED`: severity ERROR at the write sites
    (API 422 + import nested-drop), severity WARNING (explicit log, non-blocking)
    at run start.
  - **Why strict-at-run-start was rejected for the schedule:** it would brick
    runs on unsanctioned paths — direct DB writes, and any future bounds
    tightening that instantly makes existing stored policies malformed. The
    runtime resolver already fail-opens, so a warning keeps runs alive.
  - The API `_validate_retry_policy` DELEGATES to the GraphValidator (single
    source of truth, first-issue parity with byte-identical 422 details) and
    emits a NON-BLOCKING warning for unrecognized top-level keys (typo'd
    `backof_schedule` surfaces in logs; legacy `backoff` exempt).
  - Canonicalization at the write sites: integral-float `delay_seconds` →
    int, int `multiplier` → float — type-stable storage so the retry-aware
    topology hash (`compute_retry_aware_topology_hash`) does not flip. The
    topology hash folds the whole policy dict, so the new key deliberately
    participates (a schedule-only edit recompiles; harmless over-invalidation).
- **Import sanitisation is fault-classed.** `_sanitize_retry_policy` returns
  `(policy, fault_class)`: a schedule-level fault NESTED-DROPS (schedule
  removed, `on`/`max_retries`/`backoff` kept, schedule-specific warning);
  any top-level core fault WHOLE-DROPS to `{}` unchanged (legacy compat); a
  canonicalisation-only delta emits no "dropped to {}" warning. Mixed errors
  whole-drop (the core fault is fatal; a schedule we must drop anyway buys
  nothing by keeping the rest).
- **Out of scope (inert):** agent-level `retry_policy` and node-level
  `backoff` configuration are unchanged by this ADR.

## Fleet math / worker-slot worst case

The in-job sleep holds a worker slot. Legacy worst case per run:
45+90+180+300+300 = 915s across max_retries=5. With `delay_seconds: 300`,
`multiplier: 1.0` (fixed-at-cap): 300×5 = 1500s per run. Fleet-wide, a herd
of failing pipelines all sleeping at cap holds worker slots for 1500s each —
the 300s cap (code-held) bounds the per-run worst case, and the +25% jitter
spreads the re-dispatch stampede (bounded: with `delay_seconds: 300`, attempt
1 already computes AT the cap, so the jitter dead zone applies — computed
≥ cap clamps to exactly cap with ZERO spread, all retries re-fire together
at exactly 300s + SAQ delay).

## SAQ composition

The effective re-dispatch gap the operator observes is:
**in-job sleep + `settings.saq_retry_delay` (env-configurable floor, see
docs/configuration-reference.md) + queue wait**. The sleep happens inside the
job before the re-raise; SAQ then waits its own retry delay before
re-enqueueing.

## Resume-path asymmetry (known)

The run-start policy check lives in `_load_execution_context` (fresh
execution). A RESUMED run (awaiting_human → resume) does NOT re-run the
policy check — a policy mutated while the run was parked is only
re-validated on the next fresh dispatch. Accepted: the write sites guard
every sanctioned write path.

## Staging verification recipe

1. Create a pipeline whose `provider='custom'` model backend has a
   non-matching `fixture_map` so the agent node terminal-fails fast with
   `UnexpectedInputError` (deterministic failure).
2. Set `retry_policy = {on: ["failure"], max_retries: 1, backoff_schedule:
   {delay_seconds: 45, multiplier: 1.0}}` → observe ONE re-dispatch with a
   gap G; assert `45 ≤ G ≤ 45 + SAQ_RETRY_DELAY + ε` (ε = queue wait).
3. Set `max_retries ≥ 3` with `multiplier: 2.0` → observe growth
   45 → 90 → 180 (jitter adds up to +25%).
4. Fail-open path: psql-seed a pipeline with
   `retry_policy = {"on": ["failure"], "max_retries": 1, "backoff_schedule":
   {"delay_seconds": 1000}}` (write sites reject this; direct DB write is
   the only way in) → run proceeds, re-dispatch uses the hardcoded default,
   and the `pipeline.retry_policy_schedule_fail_open` warning appears with
   `schedule_state=failopen`.

## Rollback

Redeploy the previous image: the `backoff_schedule` key becomes DORMANT (the
old resolver ignores unknown keys; policies carrying it keep working via the
hardcoded schedule). Footnote: the pre-FAR-525 pipeline editor would silently
WIPE the unknown key on the next save — re-set the schedule after rollback if
edits happened in between.

**Observable rollback triggers:** a spike in `schedule_state=failopen`
re-dispatches (counter `runs_retry_redispatch_total`), or a 422 spike on
retry-policy writes (a validator regression would surface here first).
