---
id: feat-core-run-context
prd: N/A
adr: []
code:
  - backend/src/modulo/core/run_context/__init__.py
  - backend/src/modulo/core/run_context/autonomy.py
  - backend/src/modulo/core/run_context/autonomy_telemetry.py
  - backend/src/modulo/core/pipeline_engine/executor.py
  - backend/src/modulo/core/pipeline_engine/decorator.py
  - backend/src/modulo/core/pipeline_engine/node_runner.py
unit-tests:
  - backend/tests/unit/core/run_context/test_run_context_bdd.py
  - backend/tests/unit/core/run_context/test_autonomy.py
  - backend/tests/unit/core/run_context/test_autonomy_telemetry.py
  - backend/tests/unit/core/run_context/test_decorator_resilience.py
bdd:
  - backend/tests/bdd/features/pipelines/run_context.feature
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
      an absent payload seeds an empty `input`; the payload is nested under the
      `input` key and coexists with the defaults (a same-named default is **not**
      overridden by input)
- [x] Seeded state always carries a resolvable `artifacts` key
- [x] `context_setter`-role nodes can write to the context and append a write log
- [x] Non-setter node roles (`agent`, `runner`, untyped) are refused write access
- [x] Reserved keys are stripped from context writes (with a warning) and a
      reserved-only write produces no write log; `cancellable_node` is resilient to
      DB check failures (fail-open with a logged warning)
- [x] Autonomy level resolution is clamped to the pipeline's ceiling: a
      run-context recommendation may always **lower** the effective level but is
      capped at the pinned `max_autonomy_level` (when unset, the effective
      ceiling is the pipeline default, so a recommendation can only lower
      autonomy — the FAR-1163 S0 fix for the context-setter escalation hole),
      with a safe manual-approval fallback when the default/recommendation is
      unset or invalid
- [x] Autonomy telemetry emits the expected gate-outcome event payload; failures are
      fail-open and a missing session factory is a no-op
- [x] `should_skip_hitl_review` / `should_notify_on_complete` derive from the effective
      autonomy level

## Known Gaps

- **Context writes are role-gated at the decorator layer** — enforcement is
  behavioural (node role), not a storage-level permission, so a mislabeled runtime
  is the boundary.

## QA History

- 2026-10-10: **qa-iterate product-map pass** — corrected claims drifted from
  the shipped code. The seeding and context-setter write guard are implemented
  in `core/pipeline_engine/executor.py` (`_seed_state`) and
  `core/pipeline_engine/decorator.py` — added to `code:`. Dropped the false
  "explicit input overrides defaults" claim (input is nested under the `input`
  key and coexists with defaults; `_seed_state` never merges it over the
  spread). Restated autonomy resolution as ceiling-clamped (a recommendation can
  only lower autonomy unless a `max_autonomy_level` ceiling is pinned) per the
  FAR-1163 S0 work. Cited the `.feature` file in `bdd:` (the prior `.py` steps
  citation bypassed the registered-coverage guard). Status: covered.

- 2026-08-25: **product-map review pass** — entry added to close the
  dangling `depends-on: feat-core-run-context` edge in `teams/org-entity.md`.
  Behaviours re-verified against `core/run_context/*` and its unit/BDD suites.
  Status: covered.
