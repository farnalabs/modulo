# TRANSPLANT DRAFT — do not merge as a standalone ADR

> **CONDUCTOR NOTE:** ADRs live in `Repos/devtools/adr/` (migrated out of the
> product repo 2026-09-02, FAR-434). This file is an amendment draft to
> `028-configurable-run-level-retry-backoff.md` and lands there post-merge as
> an appended **"Amendment (FAR-649)"** section — not as a new ADR number and
> not as a product-repo doc. Delete this file from the product repo once
> transplanted.

# Amendment to ADR 028: absent `on` = ALL retryable errors (FAR-649)

- **Status:** Draft (to be appended to ADR 028 after FAR-649 merges)
- **Ticket:** FAR-649
- **Date:** 2026-09-07

## Context

ADR 028 (FAR-525) made the run-level `retry_policy` shape
`{on: [...], max_retries: 0-5, backoff_schedule?}`. The validator treated an
absent `on` key as valid at write time (it defaulted to `[]`), but the runtime
resolver (`_retry_after_policy`) fail-closed on it: an absent or empty `on`
meant **no retry**, even with `max_retries > 0`. The result was a
previously-inert shape: `{max_retries: 2}` passed every write gate but never
retried anything — surprising to operators who set a budget and expected it to
apply to everything.

## Decision

An **absent `on` key (key missing, or explicitly `null`) with a valid
`max_retries` in [1, 5] means ALL retryable events** (stall, timeout,
failure, eval_failed): every event matcher evaluates. This is the intuitive
default — enabling a budget opts the pipeline into all four retryable
outcomes.

- **Explicit non-empty `on` list** = granular (unchanged).
- **Explicit `on: []`** = no retry (unchanged, backward compatible — the
  FAR-525 editor's inert no-op panel save shape still means "retry nothing").
- **Malformed `max_retries`** (non-int, bool, out of [0, 5], or 0) stays
  fail-closed (no retry), including in the all-events case; the budget
  validation runs before the event-shape branch.
- **Validator accepts explicit `null` `on`** (`GraphValidator.check_retry_policy`):
  `on: null` is now treated IDENTICALLY to an absent key — valid, with the
  all-events default. This is a deliberate fix, not a regression: the shipped
  OpenAPI text already documented "absent (or null) = ALL retryable events",
  but the validator rejected an explicit `null` as RETRY_POLICY_MALFORMED —
  a 422 at the write sites and (via the run-start gate re-running the same
  check) a GraphValidationError that BRICKED legacy/hand-edited null rows at
  run start. Those rows now run with all-events retries. The validator shape
  is otherwise unchanged: no new error codes, and a non-list non-null `on`
  (string, int) stays malformed.
- **Node-level inheritance is breadth-independent**
  (`_policy_from_pipeline_default`): an absent-`on` policy with a valid budget
  (int, non-bool, 1-5) now inherits at the NODE level with all node retry
  events — coverage-equivalent to the explicit four-event run-level list,
  which maps to that same node set (same attempt ceiling `max_retries + 1`,
  same backoff). The editor's All-errors default shape is therefore equivalent
  to the explicit four-event list at BOTH the run and the node level;
  previously the absent-`on` shape silently yielded ZERO node-level retries.
  Explicit `on: []`, a malformed non-list non-null `on`, and a malformed /
  out-of-bounds budget still fail-closed to no node retry.
- **Nodeless zombie repair re-classified** (`_should_redispatch_nodeless`):
  an absent-`on` policy with a valid budget > 0 is now stall-covered, so the
  repair honors the POLICY budget (terminal-fail once exhausted) instead of
  the budget-default (`SAQ_NODELESS_REDISPATCH_BUDGET`). Explicit `on: []`,
  `{}`, no policy, and malformed/0 budgets keep the budget-default repair;
  non-empty-`on`-without-stall keeps terminal-fail.
- **Editor default flips** (FAR-649 UI): enabling retry defaults to an
  "All errors" mode that saves the policy WITHOUT the `on` key; a
  "Choose specific errors" mode saves the explicit list. Granular with zero
  selected events blocks save with a visible warning (no silent inert
  policy). Loading a policy without `on` renders All-errors; `on: []`
  renders granular-with-none-selected plus the warning.
- **Import sanitisation unchanged in effect:** `{max_retries: 2}` already
  round-trips through `_sanitize_retry_policy` (absent `on` is write-valid);
  pinned by test.

## Previously-inert shape semantics change (deliberate)

Pipelines with a stored `{max_retries: N > 0}` and no `on` were write-valid
but runtime-inert. They now retry EVERYTHING (up to the budget). This is the
point of the change, not a regression — but it is observable: pipelines saved
in that shape start re-dispatching after this deploy.

## Topology-hash note

`compute_retry_aware_topology_hash` folds the whole policy dict, so absent-`on`
and `on: []` policies hash differently (and an editor save that drops `on`
recompiles the graph once). Deliberate; pinned by test.

## Rollback

Reverting restores the inert-absent semantics: policies saved without `on`
stop retrying again (resolver fail-closes on absent/empty `on`), and the
nodeless repair returns to budget-default for them. The observable rollback
trigger is a retry-counter drop on pipelines whose policy lacks `on` — the
policy rows themselves need no migration in either direction.
