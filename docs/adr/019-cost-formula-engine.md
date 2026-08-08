# ADR 019  –  Cost Formula Engine + E2B Rate/Fallback Decision

**Date:** 2026-08-04
**Status:** Accepted (implemented in PR A1; the fallback-removal plan below is
dated and revisited when PR A ships)

---

## Context

`Run.total_cost_usd` was a single `Numeric(14,6)` from two hardcoded formulas:
`executor._compute_token_costs` (LLM tokens) and `node_runner._compute_sandbox_cost`
(E2B sandbox wall-clock + an agent-supplied `cost_estimate_usd` heuristic). The
multi-component cost-tracking feature (see
`docs/design/multi-component-cost-tracking.md`) makes a run's cost a sum of
named, admin-configurable components, each with a formula over a fixed param
registry. This requires a formula evaluation engine and a decision about the
E2B rate source.

## Decision 1  –  Hand-rolled 4-operator formula engine (no eval/exec, no asteval)

The formula engine is a hand-rolled tokenizer + recursive-descent parser
(~100 LOC, stdlib-only) supporting exactly `+ - * /`, unary minus, and
parentheses, over a whitelisted identifier registry. It is codified in
`backend/src/modulo/core/cost_controller/breakdown/formula.py`.

Rationale:

- `eval()` with restricted globals/locals is escapable via introspection
  (`().__class__.__bases__[0].__subclasses__()`, `__import__`), so it is
  rejected even for operator-authored formulas.
- `asteval` pulls in `sympy` and has an escape history; for a 4-operator
  grammar the review cost of a third party exceeds the ~100 LOC of a
  hand-rolled parser.
- The tokenizer cannot even produce `.`, `[`, `__`, a string literal, or a
  call  –  zero escape surface by construction.

The grammar must NOT grow in v1. A v1 grammar grow (an operator, a function, a
comparison) is a security-review event, never an implementation-time
convenience.

## Decision 2  –  E2B rate/fallback authority

- `Settings.e2b_sandbox_usd_per_hour` (env `E2B_SANDBOX_USD_PER_HOUR`,
  `ge=0`) is the runtime authority for the E2B hourly rate. `constants.py`
  holds ONLY defaults; runtime reads flow through `get_settings()`.
- A component may declare `rate_fallback="e2b_rate"`; the engine resolves
  `e2b_rate` from `get_settings().e2b_sandbox_usd_per_hour` when `rate_usd` is
  null. An explicit `rate_usd` wins. `e2b_rate` is the ONLY registered
  fallback name  –  the registry is exactly `{"e2b_rate"}`, and CRUD rejects any
  other name with a 422 listing the valid names.
- `node_runner._E2B_SANDBOX_USD_PER_HOUR`/`_compute_sandbox_cost` stay for the
  LEGACY fallback path only, routed through `get_settings()` at RUNTIME (a
  real code change from the import-time read). The legacy fallback DE-TRUSTS
  agent-supplied `cost_estimate_usd`  –  its total is server-verified wall-clock
  only, so it is attacker-safe.
- `E2B_SANDBOX_USD_PER_HOUR` is marked deprecated in the config reference but
  is NOT removed in this delivery.

## Decision 3  –  Dated removal of the empty-set legacy fallback + the env fallback

The empty-set legacy fallback (`llm_tokens` + one `sandbox_infra` entry with
the server-verified wall-clock total) stays as the rollout blast shield.
Removing it is a DELIBERATE behavior-change gate, not a refactor  –  this ADR
records the behavior-drift cost: after removal, an empty enabled component set
(or a cost-path exception) can no longer produce a legacy total; the run would
finalize with a zero total + log instead.

- **ADR 019 fallback-removal analysis lands by 2026-10-31**, BEFORE the
  **2026-11-30** removal window (both dates are the §11 register's single
  source). **Both are revisited when PR A ships**  –  a PR A slip past the ADR
  date makes the analysis window unmeetable, and the fallback-removal date must
  be re-derived from the ACTUAL deploy date (a fallback-removal window that has
  already passed by the time PR A lands is a silently-broken follow-up).
- The `E2B_SANDBOX_USD_PER_HOUR` env fallback is scheduled for removal
  **2026-12-31**.

## Consequences

- Operator-authored formulas are constrained to the fixed grammar  –  a malformed
  formula is rejected at save time with a 422, never evaluated.
- The E2B rate is single-sourced via Settings; an env override moves the
  boundary everywhere.
- The legacy fallback and the env fallback remain for the documented rollout
  window and are removed on the dated schedule above.
