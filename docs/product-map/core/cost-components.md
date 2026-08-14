---
id: feat-core-cost-components
prd: 8.10
delivery-tasks: []
bdd: []
code:
  - backend/src/modulo/api/routes/cost_components.py
  - backend/src/modulo/core/cost_controller/breakdown/formula.py
  - backend/src/modulo/core/cost_controller/breakdown/aggregate.py
  - backend/src/modulo/db/models/cost_component.py
  - backend/src/modulo/db/crud/cost_component.py
  - backend/src/modulo/db/migrations/versions/0066_cost_components.py
  - backend/src/modulo/core/seed_data/cost_components.py
  - backend/src/modulo/core/pipeline_engine/node_runner.py
depends-on: [feat-teams-team-crud]
unit-tests:
  - backend/tests/unit/core/cost_controller/test_cost_components_crud.py
  - backend/tests/unit/core/cost_controller/test_cost_formula.py
  - backend/tests/unit/core/cost_controller/test_cost_aggregate.py
  - backend/tests/unit/core/cost_controller/test_cost_extraction.py
  - backend/tests/integration/test_migration_0066_cost_components.py
status: partial
---

# Cost Components

Multi-component cost tracking (PRD 8.10, "Multi-Component Cost Tracking"): a
run's cost is a sum of named **cost components**, each with a rate/value, a
formula, and a kind. The `cost_components` table (migration 0066) plus the
admin CRUD route (`/api/v1/admin/costs/components`) that manages them, the
formula engine that evaluates `calculated` components, the seeds that populate
default components per org, and the node_runner self-report extraction path
that feeds the engine.

## Behaviours

### Cost component CRUD (`/api/v1/admin/costs/components`)

- [x] Create: org-scoped `POST` returns 201 with the created component
- [x] Create: `cost.manage` permission + `admin_cost_breakdown` feature required (operator denied)
- [x] Create: duplicate active name/report_key returns 409
- [x] Create: org component cap enforced (422)
- [x] Create: reserved name (`reported`, `rate`, `cost_estimate_usd`, `model_cost_usd`) rejected 422
- [x] Create: `self_reported` requires `report_key`; `calculated` must have `report_key` NULL
- [x] Create: `calculated` requires a non-empty formula (422 otherwise)
- [x] Create: unknown `rate_fallback` rejected 422 (registry)
- [x] Create: `rate_usd` above the dynamic Settings knob rejected 422
- [x] List: `GET` returns org-scoped components (RLS)
- [x] Update: `PUT` partial update via `exclude_unset`, explicit `rate_usd: None` clears the field
- [x] Update: merged-state cross-field validation re-runs (422)
- [x] Update: last enabled `calculated` component cannot be deleted/disabled/kind-changed (422)
- [x] Delete: soft delete returns 204; audit events emitted on create/update/delete

### Formula engine (`validate_formula` / `evaluate_formula`)

- [x] 4-operator grammar (`+ - * /`, unary minus, parens); no functions, no `**`, no attribute access
- [x] Unknown identifier rejected 422 (`unknown_identifier`)
- [x] Formula length capped (422 `formula_too_long`); empty expression rejected
- [x] Eval-time failures raise `CostFormulaError` (non-finite, negative result, missing param)
- [x] `validate_formula` is the SINGLE validate path — runs at save time AND eval time

### Self-report extraction (`_extract_reported_cost` in node_runner)

- [x] Reads `model_cost_raw_usd` (pre-clamp) falling back to `model_cost_usd`
- [x] Returns `None` for non-dict, non-numeric, bool, NaN/Inf, negative, zero
- [x] Clamps at the per-node cap and the band ceiling; `out_of_band_high` marker

### Seeding

- [x] Default components seeded per org at startup AND on org creation (idempotent)
- [x] Org enumeration in system context; per-org inserts under `set_rls_org`
- [x] Soft-deleted seed names are not re-created

## Known Gaps

- Per-agent `token_budget` and per-run `run_budget` (PRD 8.10) not yet implemented — see `cost-breakdown.md`
- `rate_usd` environment-fallback resolution for calculated components
- Formula evaluation is eval-time only for `calculated`; `self_reported` formulas are implicit `reported`
- No BDD feature file covers cost-component CRUD/formula/self-report extraction — `costs/cost_controls.feature` covers spend limits and the admin cost report (linked to `feat-core-cost-breakdown`), not component management. Coverage is via the unit suites listed in `unit-tests:`.
