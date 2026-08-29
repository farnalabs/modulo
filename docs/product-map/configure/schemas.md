---
id: feat-schemas
prd: N/A
adr: []
code:
  - backend/src/modulo/api/routes/schemas.py
  - backend/src/modulo/api/routes/parameter_schemas.py
  - backend/src/modulo/api/routes/schema_folders.py
  - backend/src/modulo/core/schema_registry/
  - backend/src/modulo/db/crud/schema.py
  - backend/src/modulo/db/crud/parameter_schema.py
  - frontend/src/views/SchemaListView.vue
  - frontend/src/views/SchemaEditorView.vue
  - frontend/src/views/SchemaInferenceView.vue
  - frontend/src/views/ParameterSchemasView.vue
unit-tests:
  - backend/tests/unit/api/test_schemas_endpoint.py
  - backend/tests/unit/api/test_schema_infer_endpoint.py
  - backend/tests/unit/api/test_schema_generate_endpoint.py
  - backend/tests/unit/api/test_parameter_schemas_endpoint.py
  - backend/tests/unit/api/test_parameter_schemas_idor_read.py
  - backend/tests/unit/core/test_schema_generation.py
  - backend/tests/unit/core/test_schema_inference.py
  - backend/tests/unit/core/test_schema_migration.py
  - backend/tests/unit/core/test_schema_sanitize.py
  - backend/tests/unit/core/test_schema_validation.py
bdd:
  - backend/tests/bdd/features/schemas/
depends-on:
  - feat-pipelines
status: covered
---

# Typed Schemas, Inference & Migration

Org-scoped, versioned (semver) JSON Schema definitions that form the typed seams
between pipeline stages (core principle §1 — "Schema seams"). A schema is a named
container whose JSON definitions live in immutable, auditable `SchemaVersion`s;
pipelines pin a specific version at snapshot time. Supports connector-driven
inference, generation, dry-run migration plans, deletion protection, folder
organisation and parameter schemas. Surfaces: `/schemas`,
`/schemas/editor/:id`, `/schemas/infer`, `/admin/parameter-schemas`
(`feat-schemas`).

## Behaviours

- [x] Schema CRUD: `POST /api/v1/schemas` creates (`{name, description,
      abstract_name}`; unique per org, 409 on duplicate), `GET` list/counts and
      detail read, `PATCH` updates, and org ownership is asserted on every read —
      a foreign-org schema is a 404, never a 403 leak (`create.feature`,
      `test_parameter_schemas_idor_read.py`, `_assert_owns_schema`)
- [x] Versions: each schema publishes immutable `SchemaVersion`s; updating the
      definition creates the next version, `/versions` lists all and
      `/versions/{v}` returns a specific snapshot; a run snapshot pins the exact
      version so later edits never change what ran (`version.feature`)
- [x] Deletion protection: an in-use schema (referenced by a published pipeline)
      is refused with 409 `SchemaDeletionProtectedError`; `force=true` bypasses
      the guard, and an unused schema deletes cleanly (204)
      (`deletion_protection.feature`, `db/crud/schema.py`)
- [x] Deprecation and folders: `PATCH /schemas/{id}/deprecate` marks a schema
      deprecated and `PATCH /schemas/{id}/folder` moves it between folders
      (`routes/schemas.py`, `routes/schema_folders.py`)
- [x] Inference: `POST /api/v1/schemas/infer` samples a connector instance
      (default limit 200, selectable by uuid), detects field types
      (string/number/boolean/array), suggests enum constraints for low-cardinality
      fields, flags rarely-used fields, and returns `definition_json` plus a
      `suggestion_name` (`schema_inference.feature`,
      `core/schema_registry/inference.py`)
- [x] Generation: `POST /api/v1/schemas/generate` drafts a schema from a prompt
      via a configured model backend (`schema_generate_endpoint` +
      `core/schema_registry/generation.py`, `test_schema_generation.py`)
- [x] Migration: `POST /api/v1/schemas/migrate` dry-runs or applies a migration
      plan between definitions (additions, renames, drops with best-effort
      partial-chain application), and `/migrate/plan` previews the plan and
      records `schema_migration_planned`/`schema_migration_completed` audit
      events (dry-run included) (`schema_migration.feature`,
      `core/schema_registry/migration.py`)
- [x] Validation at the seam: definitions validate against JSON Schema; field
      listing (`/schemas/{id}/fields`) and a `/validate` endpoint surface
      draft-07/draft-2020-12 conformance (`routes/schemas.py`,
      `core/schema_registry/validation.py`, `test_schema_validation.py`)
- [x] Parameter schemas: `/api/v1/parameter-schemas` CRUD drives reusable
      run-parameter sets scoped to the org (`routes/parameter_schemas.py`,
      `test_parameter_schemas_endpoint.py`)
- [x] Frontend surfaces render management (`SchemaListView.vue`), editing
      (`SchemaEditorView.vue`), inference (`SchemaInferenceView.vue`) and
      parameter-schema configuration (`ParameterSchemasView.vue`)

## Known Gaps

- **Create-time JSON Schema validation is not enforced** — `POST /schemas`
      accepts `{name, description, abstract_name}` only; definitions are not
      validated as JSON Schema until a version is published (the
      `@awaiting-implementation` scenario in `create.feature` marks this).
- **ML-based inference depends on a configured model backend** — schema
      generation/inference need an available backend; inference degrades to
      heuristic-only when none is configured.
- **Migration is field-path based** — nested-object renames are best-effort;
      cross-nesting structural rewrites are handled via partial-chain application
      rather than full restructuring.

## QA History

- 2026-08-29: **improve-architecture (product-map walk)** — added this
  behaviour-tracker for the registered manifest feature `feat-schemas`, which had
  no `docs/product-map/` entry. Behaviours verified against
  `api/routes/schemas.py` + `parameter_schemas.py` + `schema_folders.py`,
  `core/schema_registry/`, `db/crud/{schema,parameter_schema}.py`, the five
  `schemas/` BDD feature files and the schema unit/IDOR suites. Status: covered.