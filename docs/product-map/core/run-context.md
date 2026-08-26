---
id: feat-core-run-context
prd: N/A
adr: []
code:
  - backend/src/modulo/core/run_context/__init__.py
  - backend/src/modulo/core/run_context/autonomy.py
  - backend/src/modulo/core/run_context/autonomy_telemetry.py
unit-tests:
  - backend/tests/unit/core/run_context/test_run_context_bdd.py
  - backend/tests/unit/core/run_context/test_autonomy.py
  - backend/tests/unit/core/run_context/test_autonomy_telemetry.py
  - backend/tests/unit/core/run_context/test_decorator_resilience.py
bdd:
  - backend/tests/bdd/steps/test_run_context.py
depends-on: []
status: covered
---

# Run Context

The per-run mutable context object seeded at run start from pipeline defaults and
extended at runtime, plus autonomy-level resolution that drives HITL-gate /
notification behaviour. Only `context_setter` nodes write to it; autonomy
telemetry emits gate-outcome events for observability.

## Behaviours

- [x] Run context is seeded from pipeline defaults plus the trigger input payload;
      an absent payload seeds an empty `input`; explicit input overrides defaults
- [x] Seeded state always carries a resolvable `artifacts` key
- [x] `context_setter`-role nodes can write to the context and append a write log
- [x] Non-setter node roles (`agent`, `runner`, untyped) are refused write access
- [x] Reserved keys are stripped from context writes (with a warning) and a
      reserved-only write produces no write log; `cancellable_node` is resilient to
      DB check failures (fail-open with a logged warning)
- [x] Autonomy level resolution: run-context recommendation takes priority over the
      pipeline default, with a safe manual-approval fallback when unset or invalid
- [x] Autonomy telemetry emits the expected gate-outcome event payload; failures are
      fail-open and a missing session factory is a no-op
- [x] `should_skip_hitl_gate` / `should_notify_on_complete` derive from the effective
      autonomy level

## Known Gaps

- **Context writes are role-gated at the decorator layer** — enforcement is
  behavioural (node role), not a storage-level permission, so a mislabeled runtime
  is the boundary.

## QA History

- 2026-08-25: **improve-architecture (product-map walk)** — entry added to close the
  dangling `depends-on: feat-core-run-context` edge in `teams/org-entity.md`.
  Behaviours re-verified against `core/run_context/*` and its unit/BDD suites.
  Status: covered.
