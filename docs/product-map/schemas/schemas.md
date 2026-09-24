---
id: feat-schemas
prd: N/A
adr: []
code:
  - backend/src/modulo/api/routes/schemas.py
  - backend/src/modulo/api/routes/parameter_schemas.py
  - backend/src/modulo/core/schema_registry
unit-tests:
  - backend/tests/unit/api/test_schemas_endpoint.py
  - backend/tests/unit/api/test_schema_infer_endpoint.py
  - backend/tests/unit/api/test_schema_generate_endpoint.py
  - backend/tests/unit/api/test_parameter_schemas_endpoint.py
  - backend/tests/unit/core/test_schema_validation.py
  - backend/tests/unit/core/test_schema_inference.py
  - backend/tests/unit/core/test_schema_migration.py
  - backend/tests/unit/core/test_schema_sanitize.py
  - backend/tests/unit/core/test_schema_generation.py
  - backend/tests/unit/db/test_schema.py
bdd:
  - backend/tests/bdd/features/schemas/create.feature
  - backend/tests/bdd/features/schemas/version.feature
  - backend/tests/bdd/features/schemas/deletion_protection.feature
  - backend/tests/bdd/features/schemas/schema_inference.feature
  - backend/tests/bdd/features/schemas/schema_migration.feature
  - backend/tests/bdd/features/agents/schema_assignment.feature
  - backend/tests/bdd/steps/test_alpha_schemas.py
  - backend/tests/bdd/steps/test_alpha_agents.py
  - backend/tests/bdd/steps/test_schema_inference.py
  - backend/tests/bdd/steps/test_schema_migration.py
  - backend/tests/bdd/steps/test_schemas.py
depends-on:
  - feat-connectors
status: covered
---

# Typed JSON Schemas

Typed JSON Schemas define the contracts between pipeline stages, exposed through the
`/schemas` list + editor, `/schemas/infer`, and `/admin/parameter-schemas` surfaces.
Schemas are versioned (updates create new versions), are pinned by pipeline snapshots so
runs keep the schema version they were authored against, carry deletion protection while
a pipeline references them, and support connector-driven inference plus safe dry-run /
applied migration between versions (`core/schema_registry/*`).

## Behaviours

- [x] A schema is created with a name that must be unique within the org — duplicate
      name is 409, cross-org read is 404 (`create.feature`)
- [x] Create accepts an optional initial `definition_json`; a structurally-invalid
      JSON Schema (Draft 2020-12, the same gate as `/validate` and `/import`) is
      rejected with 422 before any write, and a valid definition seeds the `latest`
      placeholder version so agents have something to pin (`create.feature`)
- [x] Updating a schema creates a new version rather than mutating in place; versions
      list and per-version retrieval are exposed (`version.feature`)
- [x] A pipeline snapshot pins the schema version; a later schema update does not change
      the version a pinned run uses (`version.feature`)
- [x] Deletion protection: an unused schema is deleted (204); a schema referenced by a
      pipeline is refused with 409 and an "in use by pipeline" error, and `force=true`
      bypasses the protection (`deletion_protection.feature`)
- [x] Inference at `/api/v1/schemas/infer` builds a `definition_json` draft from a
      connector instance's sample records with a default sample limit of 200, detects
      field types and suggests enums for constrained fields, flags rarely-used fields,
      and can be published as a schema version (`schema_inference.feature`)
- [x] Migration: `/api/v1/schemas/migrate` dry-runs a plan without mutating data, a
      `/migrate/plan` endpoint previews renames/additions and records `schema_migration_planned`
      audit events, applying a migration transforms data and drops removed fields while
      recording `schema_migration_completed`, and best-effort migration of a partial chain
      applies the reachable prefix and reports chain gaps (`schema_migration.feature`)
- [x] Runtime input/output validation and sanitisation of schema definitions live in
      `core/schema_registry/validation.py` / `sanitize.py` and are unit-covered
      (`test_schema_validation.py`, `test_schema_sanitize.py`)
- [x] Agent input/output schema bindings are (re)assignable and detachable via
      `PATCH /api/v1/agents/{id}`: an omitted version resolves to the org's
      `latest` placeholder version, an explicit `null` clears both id and version,
      a version sent without an id is dropped, and a `(id, version)` pair that does
      not resolve to an org-owned schema version is a 422
      (`schema_assignment.feature`, `test_update_agent_reassigns_input_output_schemas`,
      `test_update_agent_detaches_output_schema`)
- [x] Parameter-schema CRUD for pipeline parameters is an admin surface under the same
      feature (`api/routes/parameter_schemas.py`, `test_parameter_schemas_endpoint.py`)

## Known Gaps

- **Schema inference requires a configured model backend** — the draft-building pass is
  model-assisted; there is no purely heuristic fallback inference path.

## QA History
- 2026-09-23: **product-map review pass** — implemented agent input/output
  schema (re)assignment and detachment on `PATCH /api/v1/agents/{id}` and closed
  the `schema_assignment.feature` "Remove schema assignment"
  `@awaiting-implementation` pin. `AgentUpdate` now exposes
  `input_schema_id`/`output_schema_id` (+ version fields); an omitted version
  resolves to the org's `latest` placeholder version (create parity), an explicit
  `null` detaches both id and version, a version-only entry is dropped, and a
  non-resolvable `(id, version)` pair is a 422 (`_resolve_schema_binding` /
  `_normalise_schema_updates`). BDD step `remove_input_schema` now drives the
  real PATCH route and `PINNED_AWAITING_IMPLEMENTATION` shipped that scenario;
  unit `test_update_agent_reassigns_input_output_schemas` /
  `test_update_agent_detaches_output_schema` pin the semantics (the former
  immutable-schema test asserted the now-shipped gap).
- 2026-09-20: **product-map review pass** — closed the
  `deletion_protection.feature` naming drift: the scenario title "Schema used only
  by unpinned pipeline can be deleted" contradicted its own 409 assertion (an
  unpublished pipeline still protects the schema). Renamed to "Schema referenced by
  an unpublished pipeline cannot be deleted" so the title matches the asserted and
  shipped behaviour.
- 2026-09-13: **product-map review pass** — closed the
  `create.feature` "invalid JSON Schema rejected at create" gap: the create
  endpoint now accepts an optional initial `definition_json`, applies the same
  Draft 2020-12 `check_schema` gate as `/validate` and `/import` (422 before any
  write), seeds the `latest` placeholder version with a valid supplied
  definition, and the formerly `@awaiting-implementation` BDD scenario now
  executes. Unit `test_create_schema_rejects_invalid_initial_definition` /
  `test_create_schema_with_valid_initial_definition_seeds_latest_version` pin
  the semantics.
- 2026-09-12: **product-map review pass** — registered the shared
  `JsonViewer` surface (`components/shared/JsonViewer.vue` static testids
  `json-viewer` / `json-viewer-{copy,expand-all,collapse-all,string-expand,string-collapse}`)
  in the manifest `elements:` inventory for `/schemas/infer`: the raw inferred
  definition renders inline with `<JsonViewer :show-toolbar="true">`
  (`SchemaInferenceView.vue`), so the viewer shipped in the DOM while staying
  invisible to Assistant's docs indexer / `/api/v1/manifest`. The component is now part
  of the route's reverse testid-coverage guard
  (`test_mapped_route_elements_cover_owning_view_testids`).

- 2026-09-12: **product-map review pass** — registered the shared
  plan-entitlement gate surface (`components/FeatureGate.vue` + `LockIcon.vue` static
  testids `feature-gate*` / `lock-icon`) in the manifest `elements:` inventory for `/schemas/editor/:id`
  and wired the two components into the reverse testid-coverage guard
  (`test_mapped_route_elements_cover_owning_view_testids`), so the entitlement-card
  surface on those pages stays visible to Assistant's docs indexer / `/api/v1/manifest` and
  can no longer drift unguarded.

- 2026-09-11: **product-map review pass** — registered the shared
  search-bar surface (`components/shared/FilterBar.vue` static testids
  `filter-bar-search` / `filter-bar-search-wrapper`) in the `/schemas/editor/:id`
  manifest `elements:` inventory and wired the component into the reverse
  testid-coverage guard, so the schema search control the page ships stays visible
  to Assistant's docs indexer and `/api/v1/manifest`.

- 2026-09-11: **product-map review pass** — extended the reverse
  testid-coverage guard (`test_mapped_route_elements_cover_owning_view_testids`) to
  `/admin/parameter-schemas, /schemas/editor/:id, /schemas/infer`: the whole-page view(s) `ParameterSchemasView.vue, SchemaEditorView.vue, SchemaInferenceView.vue` render static `data-testid`s that the
  product map `elements:` inventory already documents, but the surface was not yet
  guarded against drift. The route now maps to its owning view so a newly shipped
  testid can no longer silently stay invisible to Assistant's docs indexer /
  `/api/v1/manifest`.

- 2026-09-11: **product-map review pass** — registered the
  schema folder tree (`pipelines/FolderTree.vue` static testids `folder-tree`,
  `folder-tree-new`, `folder-tree-all-pipelines`) in the `/schemas` manifest
  `elements:` inventory — `SchemaListView.vue` renders the shared folder tree on the
  schema library page, but none of its static testids were in the product map.
  `test_mapped_route_elements_cover_owning_view_testids` now maps `/schemas` to
  `SchemaListView.vue` + `FolderTree.vue` so the folder surface cannot drift
  invisible to Assistant's docs indexer / `/api/v1/manifest`.
- 2026-08-27: **product-map review pass** — added this behaviour-tracker
  for the registered manifest feature `feat-schemas`, which previously had no
  `docs/product-map/` entry. Behaviours verified against `api/routes/schemas.py`,
  `core/schema_registry/*` and the schemas BDD/unit suites. Status: covered.
