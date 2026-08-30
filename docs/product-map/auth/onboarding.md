---
id: feat-onboarding
prd: N/A
adr: []
code:
  - backend/src/modulo/api/routes/onboarding.py
unit-tests:
  - backend/tests/unit/api/test_onboarding.py
bdd:
  - backend/tests/bdd/features/onboarding/sdlc_onboarding.feature
depends-on:
  - feat-model-backends
  - feat-schemas
status: covered
---

# Onboarding

First-run onboarding wizard (`/onboarding`) driven by an action-based checklist with
DB persistence (`OnboardingProgress`). Six org-scoped actions — log in, add an AI
model, create first agent, create first schema, create first pipeline, run first
pipeline — are auto-completed from real org state, or manually completed/skipped, and
the whole wizard can be dismissed. Rapid-start helpers seed a "Truth Classifier"
example (schema, schema version, agent, pipeline) and create a starter pipeline.

## Behaviours

- [x] `GET /api/v1/onboarding/status` returns `is_first_run`, `progress_pct`,
      `completed_actions`, `skipped_actions`, `dismissed` and the ordered action list;
      the progress row is created lazily on first read
- [x] Auto-completion: `login` is always completed, and `add_ai_model` /
      `create_first_agent` / `create_first_schema` / `create_first_pipeline` /
      `run_first_pipeline` auto-complete when the org actually has a model backend,
      agent, schema, pipeline or run respectively (`_check_auto_completion`)
- [x] `POST /actions/{id}/complete` marks an action complete (idempotent, removes it
      from skipped) and `POST /actions/{id}/skip` marks it skipped; invalid ids reject
      with 422 and a list of valid ids; both recompute `progress_pct`
- [x] `POST /dismiss` persists the dismissal so the wizard no longer shows as first-run
- [x] `POST /seed-examples` creates the {name="Truth Classifier"} schema + v1.0
      published definition, a "Statement Input" schema + version, an executable agent
      bound to the org's first model backend (agent creation is skipped when no model
      backend exists), and (if present) an example pipeline
- [x] `POST /starter-pipeline` creates a starter pipeline for the org
- [x] All mutations run under org RLS (`set_rls_org` / `set_rls_user_context`); the
      seed path requires `pipeline.create` + `agent.create` + `schema.create` permits
      (`test_onboarding.py`)

## Known Gaps

- **BDD drift** — `sdlc_onboarding.feature` describes a 5-step SDLC wizard
  (`connect_tools` → `run_inference` → `review_schemas` → …) with a
  `GET /api/v1/onboarding/step/connect_tools` endpoint that does not exist; the shipped
  API is the 6-action checklist above. The feature file is red-herring coverage.
- **No PRD section reference** — onboarding has no single PRD section mapped in code
  or ADRs.
- **Seed truncation** — `seed-examples` cannot create the example agent when no model
  backend is configured, so part of the seed silently degrades on fresh orgs.

## QA History

- 2026-08-28: **improve-architecture (product-map walk)** — added this behaviour-tracker
  for the registered manifest feature `feat-onboarding`, which previously had no
  `docs/product-map/` entry. Behaviours verified against `api/routes/onboarding.py` and
  `test_onboarding.py`. Status: covered.