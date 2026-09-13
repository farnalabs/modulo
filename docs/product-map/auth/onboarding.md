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
      bound to the org's first model backend, and an example pipeline interlinked with
      that agent; when the org has no model backend the seed refuses with a 409 and a
      message pointing at the "Add an AI Model" step instead of silently seeding a
      partial, agent-less example (no writes are performed on refusal)
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

## QA History

- 2026-09-13: **improve-architecture (product-map walk)** — closed the "seed
  truncation" gap in `POST /seed-examples`: the endpoint previously created the
  schemas + pipeline and silently skipped the agent + pipeline graph when the org
  had no model backend (`agent_id: null` in a 201 response, no lint anywhere).
  It now refuses the seed with a 409 and an actionable detail pointing at the
  "Add an AI Model" step, before any write, so a fresh org either gets a complete
  executable example or nothing. Behaviour bullet updated; unit
  `test_seed_examples_no_model_backend` now asserts the 409 reject-and-refuse
  semantics.
- 2026-09-13: **improve-architecture (product-map walk)** — corrected the
  onboarding action deep links against the manifest (ADR 008 source of truth):
  `add_ai_model` pointed at `/settings/model-backends`, `create_first_agent` at
  `/agents/create`, `create_first_schema` at `/schemas/create` and
  `create_first_pipeline` at `/pipelines/create` — none of which exist in
  `frontend/src/manifest.yaml`. The onboarding banner's click-to-navigate
  silently swallowed them via the router `/:pathMatch(.*)*` redirect, landing
  users back on the dashboard. Each now targets a shipped route in the registry:
  `/admin/model-backends`, `/pipelines` (agent authoring surface), `/schemas/infer`
  (create/infer first schema) and `/library` (the "new pipeline" affordance).
- 2026-09-11: **improve-architecture (product-map walk)** — extended the reverse
  testid-coverage guard (`test_mapped_route_elements_cover_owning_view_testids`) to
  `/onboarding`: the whole-page view(s) `OnboardingWizard.vue` render static `data-testid`s that the
  product map `elements:` inventory already documents, but the surface was not yet
  guarded against drift. The route now maps to its owning view so a newly shipped
  testid can no longer silently stay invisible to Remy's docs indexer /
  `/api/v1/manifest`.

- 2026-09-11: **improve-architecture (product-map walk)** — registered the
  app-layout onboarding surface in the `/` manifest `elements:` inventory: the
  produced/consumed banner (`onboarding/OnboardingBanner.vue` static testids
  `onboarding-banner-trigger`, `onboarding-banner-checklist`,
  `onboarding-skip-action`, `onboarding-dismiss`, `onboarding-seed-examples`)
  and the first-run spotlight (`onboarding/SpotlightOverlay.vue`'s
  `spotlight-overlay`) render on the dashboard via `AppLayout.vue` but had no
  product-map home. `test_mapped_route_elements_cover_owning_view_testids` now
  maps `/` to both components so a newly shipped banner/spotlight testid cannot
  drift invisible to Remy's docs indexer / `/api/v1/manifest`.
- 2026-08-28: **improve-architecture (product-map walk)** — added this behaviour-tracker
  for the registered manifest feature `feat-onboarding`, which previously had no
  `docs/product-map/` entry. Behaviours verified against `api/routes/onboarding.py` and
  `test_onboarding.py`. Status: covered.
