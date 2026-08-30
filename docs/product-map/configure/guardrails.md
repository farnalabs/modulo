---
id: feat-guardrails
prd: N/A
adr: []
code:
  - backend/src/modulo/core/guardrails/__init__.py
  - backend/src/modulo/core/guardrails/config.py
  - backend/src/modulo/core/guardrails/loop_intercept.py
  - backend/src/modulo/core/guardrails/compensation.py
  - backend/src/modulo/core/guardrails/correction.py
  - backend/src/modulo/api/routes/guardrail_config.py
  - frontend/src/views/SettingsGuardrailsView.vue
unit-tests:
  - backend/tests/unit/core/test_guardrails.py
  - backend/tests/unit/core/test_guardrails_contract.py
  - backend/tests/unit/test_guardrail_loop_intercept.py
  - backend/tests/unit/test_guardrail_compensation.py
  - backend/tests/unit/test_guardrail_correction.py
  - backend/tests/unit/test_guardrails_kill_switch.py
  - backend/tests/unit/db/test_guardrail_override_e2e.py
  - backend/tests/integration/test_guardrail_loop_intercept.py
  - backend/tests/integration/test_guardrail_compensation.py
  - backend/tests/integration/test_guardrail_correction.py
  - backend/tests/integration/test_guardrail_config_api.py
  - frontend/src/__tests__/SettingsGuardrailsView.spec.ts
bdd:
  - backend/tests/bdd/features/evals/guardrails.feature
  - backend/tests/bdd/features/evals/guardrail_config.feature
  - backend/tests/bdd/steps/test_guardrails_steps.py
  - backend/tests/bdd/steps/test_guardrail_config_steps.py
depends-on: [feat-evals]
status: covered
---

# Guardrails

Structured-credential boundary data-safety at the run's ingestion edge. A
guardrail is an `EvalDefinition` row with `eval_type="guardrail"` whose
detection is deterministic and pure — only `regex` and `json_schema` eval
types may be bound as guardrails — and which acts on a run with one of four
actions: **observe**, **warn**, **block**, or **redact**. Guardrails wrap the
configuration surface under `/settings/guardrails`, the git-style config-as-code
workflow, the sandbox agent-loop interception bridge, run-termination
compensation, and single-node self-correction. Built on the eval engine
(`depends-on: feat-evals`).

## Behaviours

- [x] A guardrail is bound as an `EvalDefinition` with `eval_type="guardrail"`;
      detection is DETERMINISTIC and PURE — only the `regex` and `json_schema`
      eval types are usable, and the engine raises on a `llm_judge` /
      `custom_function` guardrail misrouting
- [x] Four actions with distinct semantics: `observe` computes + validates +
      discards + logs a would-block result (shadow mode); `warn` logs the
      violation and the run continues; `block` transitions the run to the
      TERMINAL `eval_failed` state; `redact` masks-only field-scoped redaction
      at the ingestion edge — `failure_behaviour='retry'` is never expressible
      on a guardrail row (block semantics are guardrail-owned, not eval-owned)
- [x] The interception pass runs at run-creation BEFORE `input_payload` is
      persisted, in two phases: evaluate ALL bound guardrails against an
      immutable pre-act copy of the payload, then apply redaction masks in
      deterministic order; a block outcome raises `GuardrailBlockedError` which
      the interception seam maps to a terminal `eval_failed` run — persisted
      state is post-redaction
- [x] Redaction is masks-only with a fixed mask token never derived from payload
      content; field paths are STATIC author config resolved with EXACT/ANCHOR
      key matching (substring matching is forbidden) and a built-in allowlist of
      never-touch system fields is always honoured
- [x] Config-as-code (`GuardrailConfigSet`, FAR-219 T3): a versioned YAML
      source-of-truth whose entries map 1:1 to the guardrail `EvalDefinition`
      rows, validated against the engine's own rules, hashed over a canonical
      serialization (not raw YAML text, so equivalent layouts hash identically),
      and diffed; the propose → apply/reject workflow reconciles the live rows,
      and applied snapshots are pinned (`guardrail_pins_json`) for drift
      detection
- [x] REST surface (`/api/v1/guardrails/config`): `GET` config export as YAML,
      `POST propose` (validate + hash + diff), `POST apply` (approve/merge),
      `POST reject` (discard), `GET drift` (recompute vs applied pin) — every
      state-changing step is admin-gated and emits an audit event with summary
      payloads only (never raw config content)
- [x] Kill switch: an org-level guardrail kill switch disables enforcement and
      is surfaced as a banner in the `/settings/guardrails` view
- [x] Agent-loop interior interception (FAR-211 T3): a Modulo-hosted bridge
      inside `sandbox_agent` loops reports each tool invocation before execution
      and each tool result before it re-enters model context, REUSING the T1
      guardrail rows + engine — detection is never reimplemented
- [x] Run-termination compensation (FAR-213): on a guardrail-blocked
      terminalization, per-node connector compensating callbacks run
      best-effort with failure isolation (e.g. GitHub closes an opened PR), a
      `blocked_partial` run summary records executed nodes / publish status /
      output references (never duplicated raw payloads), audit events record
      the compensation — the hook never raises into terminalization
- [x] Single-node self-correction (FAR-210 T2b): a bounded, single-node recovery
      rewrites a guardrail-violating node input through a RESTRICTED model
      backend (which never receives guardrail config or vault secrets, and sees
      PRE-REDACTED input) and re-validates the produced output with a
      DIFFERENT-FAMILY detector within a single retry budget — no pipeline
      re-execution, no connector/vault access
- [x] Packs ship as versioned policy packs (e.g. SOC2 pack, policy pack) that
      bundle pre-authored guardrail definitions

## Known Gaps

- **Correction is bounded single-node only** — the whole-pipeline feedback
  correction (`spawn_correction_run`) is a separate surface; this module never
  re-runs the pipeline.
- **Loop-interception covers Modulo-hosted sandbox agent loops only** — the
  pre-execution/post-result bridge does not intercept tool calls issued inside
  external/unmediated runtimes.

## QA History

- 2026-08-30: **improve-architecture (product-map walk)** — new behaviour
  tracker for the registered `feat-guardrails` manifest feature (route
  `/settings/guardrails`, previously absent from the feature graph and invisible
  to Remy's docs indexer). Behaviours verified against
  `core/guardrails/*` (engine, config-as-code, loop-intercept, compensation,
  correction), `api/routes/guardrail_config.py`, the kill-switch + override +
  config unit/integration suites, and the `guardrails` / `guardrail_config` BDD
  features. Status: covered.