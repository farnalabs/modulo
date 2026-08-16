---
id: feat-evals-guardrails
prd: 8.17
code:
  - backend/src/modulo/core/guardrails/__init__.py
  - backend/src/modulo/core/guardrails/config.py
  - backend/src/modulo/db/crud/guardrail_config.py
  - backend/src/modulo/api/routes/guardrail_config.py
  - backend/src/modulo/db/models/organisation.py
  - backend/src/modulo/db/migrations/versions/0105_guardrail_pins.py
  - backend/src/modulo/api/main.py
unit-tests:
  - backend/tests/unit/core/test_guardrails.py
  - backend/tests/unit/core/test_guardrails_contract.py
  - backend/tests/unit/db/test_guardrails_interception.py
  - backend/tests/unit/test_guardrail_config.py
bdd:
  - backend/tests/bdd/features/evals/guardrails.feature
  - backend/tests/bdd/features/evals/guardrail_config.feature
depends-on: [feat-evals-eval-engine]
status: partial
---

# Guardrails

Boundary enforcement for agent safety: the same eval primitives as §8.17 extended to the input side. Guardrails are `EvalDefinition` rows with `eval_type="guardrail"`, evaluated deterministically (regex / json_schema only — never an LLM judge) at the ingestion edge, before run payloads are persisted. Shipped layers: **T1** the input-side interception engine (`modulo.core.guardrails`), and **T3** snapshot-pinned, PR-gated config-as-code (`modulo.core.guardrails.config` + `/api/v1/guardrails/config`).

## Behaviours

### Detection engine (T1)

- [x] Detection is deterministic and pure — regex | json_schema only; routing a guardrail through `EvalEngine.evaluate` raises `GuardrailMisroutedError`
- [x] Interception runs at run-creation inside `create_run` before `runs.input_payload` is persisted (persisted state is post-redaction)
- [x] Redaction is masks-only (fixed mask token, never payload-derived), static field paths with exact/anchor matching
- [x] `block` is terminal `eval_failed` (`error_code=eval_blocked`) — run is never dispatched, never retried, no HITL gate
- [x] `failure_behaviour='retry'` is forbidden on guardrails (rejected at the API edge and the DB CHECK); guardrail rows always write `failure_behaviour='warn'`
- [x] Observe/warn never block; observe-mode results stamp `eval_results.observed`
- [x] Guardrail-override endpoint (`POST /runs/{run_id}/guardrail-override`) is the audited remediation path; the generic recover endpoint refuses guardrail-blocked runs

### Config-as-code authoring (T3)

- [x] YAML config set (`version` + `guardrails` list) loads via `load_config_set` with engine rule validation at propose time (regex needs pattern+field, json_schema needs a schema dict, duplicate ids rejected)
- [x] Content hashes are stable SHA-256 over a canonical serialization (sorted keys, guardrails sorted by stable id, `name` excluded) — equivalent YAML layouts hash identically
- [x] Diff computes per-guardrail add / update / remove keyed by stable id
- [x] `GET /api/v1/guardrails/config` exports the applied config as YAML + pin metadata (hash, applied_at, status)

### Propose → apply → reject workflow (T3)

- [x] `POST /propose` validates + hashes a proposal, computes the diff, stores it on the pin (`status=proposed`), emits `guardrail_config.proposed` audit event (summary payload only)
- [x] `POST /apply` (operator+) reconciles the live `eval_type='guardrail'` rows across the org's non-deleted pipelines (upsert present ids, delete removed ids), moves the pin to clean applied state, emits `guardrail_config.applied`
- [x] `POST /reject` discards the pending proposal and clears the pin's proposal fields, emits `guardrail_config.rejected`
- [x] Apply/reject with no pending proposal → 409
- [x] Propose with invalid YAML or a guardrail rule violation → 422
- [x] Every state-changing step emits an audit event with summary payloads only — never raw config content

### Drift detection (T3)

- [x] `GET /api/v1/guardrails/config/drift` recomputes the applied hash from the live DB rows and reports `clean`/`drift`
- [x] Drift begins: pin status flips to `drift` and `guardrail_config.drift_detected` audit event is written once (not on every poll)
- [x] Recovered drift (rows match the pin again) flips status back to `clean`
- [x] A never-applied pin is compared against the empty set — first apply to a fresh org is never flagged, but a pinned config missing from the rows IS flagged

### Security & tenancy

- [x] Reads/writes are scoped to the caller's `organisation_id` via RLS (`set_rls_org` inside the transaction)
- [x] `eval.definition.create` permission required for propose/apply/reject; `eval.list` for get/drift
- [x] Stored proposal/snapshot YAML is never echoed in audit payloads

## Known Gaps

- [x] **RESOLVED** (2026-08-16): No BDD feature files cover the guardrails engine or config-as-code workflow. New `evals/guardrails.feature` (10 scenarios) covers the T1 detection engine — block raises `GuardrailBlockedError` on regex violation / passes clean payloads, warn never raises, redaction masks with the fixed token without mutating the source payload, non-raising interception reports the block, generic-engine misrouting raises `GuardrailMisroutedError`, forbidden `retry` failure behaviour raises `GuardrailConfigError`, and conformance derivation present/absent. New `evals/guardrail_config.feature` (11 scenarios) covers the T3 config-as-code workflow through the real `/api/v1/guardrails/config` route handlers — propose validates + hashes + diffs (add/update), malformed YAML and rule violations → 422, apply/reject with no pending proposal → 409, apply reconciles rows + reports clean, reject discards, drift reports clean/drift, and viewer propose → 403. Step files: `tests/bdd/steps/test_guardrails_steps.py` + `test_guardrail_config_steps.py`.
- [ ] Config-as-code has no frontend management UI (planned in PRD §8.17)
- [ ] Conformance enforcement wiring at dispatch time (three-state derivation shipped as a pure helper) and the kill-switch rollout flag remain planned
- [ ] `guardrail_summary` telemetry on run detail and canary guardrails remain planned

## QA History

- 2026-08-15: Coverage completion (FAR-231/FAR-233 distribute batch). Re-verified the 25 checked behaviours against `modulo/core/guardrails/`, `db/crud/guardrail_config.py`, `api/routes/guardrail_config.py`, and the guardrails/guardrail_config unit + contract + interception + config tests (88 tests). Confirmed the three unchecked items are genuine, PRD-acknowledged gaps (guardrail management UI, run-creation-time conformance enforcement wiring + kill-switch flag, `guardrail_summary` telemetry + canary guardrails — all listed as "planned" in PRD §8.17). No new [x] items this pass.

- 2026-08-16: Added BDD coverage for the guardrails subsystem (`evals/guardrails.feature` + `evals/guardrail_config.feature`, 21 scenarios across both step files) — resolves the "No BDD feature files cover the guardrails engine or config-as-code workflow" known gap. The engine feature drives the pure `modulo.core.guardrails` functions directly (raising `evaluate_guardrails`, non-raising `run_interception_pass`, `apply_redaction_masks`, `derive_conformance_state`, misrouting/retry guards); the workflow feature drives the real route handlers with DB seams double-stubbed (`get_guardrail_pin`/`set_guardrail_pin` CRUD, `_load_guardrail_definitions`, `_reconcile_guardrail_rows`). Verification: 20/20 new BDD scenarios pass, 88 guardrails unit + contract + interception + config tests pass, `check-bdd-coverage.py` no longer lists either feature file, ruff check + format clean, mypy --strict clean. Status: partial (frontend management UI, dispatch-time conformance enforcement, `guardrail_summary` telemetry, canary guardrails remain).
- 2026-08-15: Added product map entry for the guardrails subsystem (T1 engine + T3 config-as-code) — resolves the orphaned `guardrail_config.py` route module in the route→map orphan check. Wired the config-as-code code paths, unit tests, and integration test into the entry's frontmatter.
