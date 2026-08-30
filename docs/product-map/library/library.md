---
id: feat-library
prd: N/A
adr: []
code:
  - backend/src/modulo/api/routes/library.py
  - backend/src/modulo/api/routes/community_library.py
  - backend/src/modulo/core/library_service/community.py
  - backend/src/modulo/core/library_sync
unit-tests:
  - backend/tests/unit/library_service/test_library_service.py
  - backend/tests/unit/library_service/test_contribution_flow.py
  - backend/tests/unit/library_service/test_ratings.py
  - backend/tests/unit/library_service/test_composite_library.py
  - backend/tests/unit/library_sync/test_sync.py
  - backend/tests/unit/library_sync/test_community_install.py
  - backend/tests/unit/library_sync/test_library_manifest.py
  - backend/tests/unit/library_sync/test_client.py
bdd:
  - backend/tests/bdd/features/library/browse.feature
  - backend/tests/bdd/features/library/copy_to_adapt.feature
  - backend/tests/bdd/features/library/ratings.feature
  - backend/tests/bdd/features/library/tiering.feature
  - backend/tests/bdd/features/library/auto_update.feature
  - backend/tests/bdd/features/library/community_registry.feature
  - backend/tests/bdd/features/library/schemas.feature
  - backend/tests/bdd/steps/test_library.py
  - backend/tests/bdd/steps/test_community_registry.py
  - backend/tests/bdd/steps/test_schemas.py
depends-on:
  - feat-pipelines
  - feat-schemas
status: covered
---

# Pipeline Template Library

The library (`/library`, `/library/:id/create-pipeline`) is the reusable-primitive and
pipeline-template surface. Primitives (agents, schemas, workflows, connectors) are listed,
type-filtered, searched and detail-viewed; community primitives are copied into the org and
adapted (forked) with an owner team; community sync/install, contribution, ratings and
auto-update are served by `core/library_sync` + `core/library_service/community.py`; and
each primitive carries an integration tier (native / preview / in_dev) per ADR 010.

## Behaviours

- [x] Primitives list with `primitive_type`, `source` (local/community), and `search`
      filters, plus single-primitive detail (`browse.feature`)
- [x] Copy-to-adapt: POST `/api/v1/libraries/{id}/adapt` copies a community primitive
      locally with `forked_from` set, supports an optional `owner_team_id`, returns 404 for
      a missing primitive, and MCP copy requires the runner role (403 for viewers)
      (`copy_to_adapt.feature`)
- [x] Ratings on library primitives are exercised (`ratings.feature`)
- [x] Tier classification: an explicit tier (preview) persists on create, omitting the
      tier defaults to native, `in_dev` primitives are excluded from the default listing,
      `include_in_dev=true` reveals them only to authorised roles (403 for viewers)
      (`tiering.feature`)
- [x] Community sync/install and the community registry are covered
      (`community_registry.feature`, `core/library_sync`, `test_community_install.py`)
- [x] Library-schema seeding and dogfood schemas underpin create-pipeline from a template
      (`library/schemas.feature`, `test_schema_seeds.py`)

## Known Gaps

- **`library/contribute.feature` is not bound to any step file** — the community
  contribution BDD scenarios exist but are not exercised by the BDD suite (contribution is
  unit-tested only: `tests/unit/library_service/test_contribution_flow.py`).
- **`community_registry.feature` is a separate surface from contribution** — contribution
  authoring and registry browsing are tracked under one feature here but cited separately.

## QA History

- 2026-08-27: **improve-architecture (product-map walk)** — added this behaviour-tracker
  for the registered manifest feature `feat-library`, which previously had no
  `docs/product-map/` entry. Behaviours verified against `api/routes/library.py`,
  `core/library_sync`, `core/library_service/*` and the library BDD/unit suites.
  Status: covered.
