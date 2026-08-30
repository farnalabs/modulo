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
  - backend/tests/bdd/steps/test_alpha_schemas.py
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
- [x] Parameter-schema CRUD for pipeline parameters is an admin surface under the same
      feature (`api/routes/parameter_schemas.py`, `test_parameter_schemas_endpoint.py`)

## Known Gaps

- **`create.feature` "invalid JSON Schema rejected at create" is `@awaiting-implementation`**
  — the create endpoint accepts `{name, description, abstract_name}` only; JSON definitions
  live in schema versions and are not JSON-Schema-validated at create time, so there is no
  reject-invalid-against-422 behaviour at creation today.
- **Schema inference requires a configured model backend** — the draft-building pass is
  model-assisted; there is no purely heuristic fallback inference path.
- **`deletion_protection.feature` naming drift** — the "Schema used only by unpinned
  pipeline can be deleted" scenario title contradicts its own 409 assertion (an unpublished
  pipeline still protects the schema); the behaviour (deletion refused while referenced)
  is what is asserted and shipped.

## QA History

- 2026-08-27: **improve-architecture (product-map walk)** — added this behaviour-tracker
  for the registered manifest feature `feat-schemas`, which previously had no
  `docs/product-map/` entry. Behaviours verified against `api/routes/schemas.py`,
  `core/schema_registry/*` and the schemas BDD/unit suites. Status: covered.
