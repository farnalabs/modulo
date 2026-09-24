---
id: feat-library
prd: N/A
adr: []
code:
  - backend/src/modulo/api/routes/library.py
  - backend/src/modulo/api/routes/community_library.py
  - backend/src/modulo/core/library_service/community.py
  - backend/src/modulo/core/library_sync
  - frontend/src/views/CollectionCreateView.vue
  - frontend/src/views/CollectionDetailView.vue
unit-tests:
  - backend/tests/unit/library_service/test_library_service.py
  - backend/tests/unit/api/test_library_collection.py
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
  - backend/tests/bdd/features/library/contribute.feature
  - backend/tests/bdd/features/library/schemas.feature
  - backend/tests/bdd/features/composites/composite_library.feature
  - backend/tests/bdd/steps/test_library.py
  - backend/tests/bdd/steps/test_community_registry.py
  - backend/tests/bdd/steps/test_library_contributions.py
  - backend/tests/bdd/steps/test_schemas.py
  - backend/tests/bdd/steps/test_composites.py
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
Library collections (FAR-760) are authored at `/library/collections/new` and viewed at
`/library/collections/:id`, gated behind the `library_collection` feature flag.

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
- [x] Fixture contributions are exercised end to end through
      `/api/v1/library/contribute` (`contributions.py`): a `draft` is created (201),
      missing fields are rejected 422, a draft is submitted to the `review_queue`,
      a non-draft submit is 409, publish (admin-only, 403 for viewers) sets
      `visibility=community`, a published contribution is versioned into a fresh
      `draft` (201, 409 on a draft original), and contributions/versions are
      listed (`contribute.feature`, `test_library_contributions.py`)
- [x] Library-schema seeding and dogfood schemas underpin create-pipeline from a template
      (`library/schemas.feature`, `test_schema_seeds.py`)
- [x] Composite library primitives are saved/browsed/adapted, and the create and update
      boundaries validate the composite graph body: a `composite` primitive's
      `content_json` must carry `nodes` and `edges` lists, so an empty or structurally
      missing payload is rejected 422 at the route layer instead of persisting (or
      mutating into) a broken primitive
      (`composites/composite_library.feature`, `LibraryPrimitiveCreate` /
      `LibraryPrimitiveUpdate` handling in `api/routes/library.py`,
      `test_library_routes.py`)
- [x] Library collections (FAR-760): a `library_collection` primitive can be created as a
      draft (201), its manifest pins updated while draft, and published (200) — invalid
      pins, duplicate pins, an empty manifest and more than `MAX_COLLECTION_PINS` are
      rejected 422, a duplicate slug is 409, mutating a non-collection or non-draft
      primitive is 400, and every collection endpoint 404s when the `library_collection`
      feature flag is off; write requires the operator role
      (`backend/tests/unit/api/test_library_collection.py`,
      `frontend/src/views/CollectionCreateView.vue`,
      `frontend/src/views/CollectionDetailView.vue`)

## Known Gaps

- **`community_registry.feature` is a separate surface from contribution** — contribution
  authoring and registry browsing are tracked under one feature here but cited separately.

## QA History

- 2026-09-21: **product-map walk** — closed the composite
  content_json boundary gap: `composite_library.feature`'s "Composite content_json
  validation — missing required fields returns error" scenario (previously
  pinned `@awaiting-implementation`) now drives the REAL `POST /api/v1/libraries`
  create route. `LibraryPrimitiveCreate` gained a `model_validator` that rejects a
  composite payload whose `content_json` lacks the `nodes`/`edges` graph body with
  422 (previously such a payload fell through to a bogus 409 from the DB
  IntegrityError mapping). Unit coverage added in `test_library_routes.py`, and the
  scenario was removed from `PINNED_AWAITING_IMPLEMENTATION`.

- 2026-09-21: **review follow-up** — extended the composite `content_json` graph
  validation to the update boundary. `PATCH /api/v1/libraries/{id}` now fetches the
  target primitive inside the transaction and rejects a composite whose patched
  `content_json` lacks the `nodes`/`edges` lists with 422 (previously an existing
  composite could be mutated into a structurally broken graph). The shared
  `_assert_composite_content_json` helper backs both the create `model_validator`
  and the update route; unit coverage added in `test_library_routes.py`.

- 2026-09-13: **product-map review pass** — closed the contribution BDD
  gap: wired `library/contribute.feature` into the executing suite via the new
  `steps/test_library_contributions.py` (11 scenarios) and dropped the file from the
  tracked orphaned-BDD debt list (`_ORPHANED_BDD_FEATURES`). The rewritten feature
  exercises the real `/api/v1/library/contribute` routes (`contributions.py`) with only
  the DB service functions patched: draft create (201) / missing-field 422, draft →
  `review_queue` submit (409 on non-draft), admin-only publish (403 for viewers,
  `contribution.publish`), version-bump (201 draft, 409 on draft original) and the
  contribution/version list surfaces. Contribution is no longer unit-tested only.

- 2026-09-12: **product-map review pass** — registered the shared
  `PageHeader` right-slot surface (`components/shared/PageHeader.vue`, static testid
  `page-header-right`) in the manifest `elements:` inventory for `/library`, which
  renders the header's `#right` action slot, and wired the component into the reverse
  testid-coverage guard (`test_mapped_route_elements_cover_owning_view_testids`) so the
  header action surface stays visible to Assistant's docs indexer / `/api/v1/manifest`.

- 2026-09-11: **product-map review pass** — extended the reverse
  testid-coverage guard (`test_mapped_route_elements_cover_owning_view_testids`) to
  `/library/:id/create-pipeline`: the whole-page view(s) `LibraryPipelineWizard.vue` render static `data-testid`s that the
  product map `elements:` inventory already documents, but the surface was not yet
  guarded against drift. The route now maps to its owning view so a newly shipped
  testid can no longer silently stay invisible to Assistant's docs indexer /
  `/api/v1/manifest`.

- 2026-09-11: **product-map review pass** — registered the shared
  search-bar surface (`components/shared/FilterBar.vue` static testids
  `filter-bar-search` / `filter-bar-search-wrapper`) in the `/library` manifest
  `elements:` inventory and wired the component into the reverse testid-coverage
  guard, so the search control the page ships stays visible to Assistant's docs indexer
  and `/api/v1/manifest`.

- 2026-09-10: **product-map review pass** — registered the
  `library-collection-badge` testid of `LibraryPrimitiveCard.vue` in the `/library`
  manifest `elements:` inventory, so the collection-membership badge on library
  cards is no longer invisible to Assistant's docs indexer / `/api/v1/manifest`.
- 2026-09-10: **product-map review pass** — registered the collection
  authoring/detail and collections-tab testids (`collection-*`,
  `library-section-collections`, `library-create-collection`, `library-collections-error`)
  in the manifest `elements:` inventory and added `LibraryView.vue`,
  `CollectionCreateView.vue` and `CollectionDetailView.vue` to the reverse testid-coverage
  guard (`test_mapped_route_elements_cover_owning_view_testids`), so the FAR-760 collection
  surface can no longer ship controls invisible to Assistant's docs indexer / `/api/v1/manifest`.
- 2026-09-10: **product-map review pass** — added the FAR-760 library
  collections behaviour (flag-gated draft → publish lifecycle) and cited the collection
  unit test and frontend views; the graph-root registry index now lists the collection
  routes. Verified against `backend/tests/unit/api/test_library_collection.py`,
  `frontend/src/views/CollectionCreateView.vue` and `CollectionDetailView.vue`.
- 2026-08-27: **product-map review pass** — added this behaviour-tracker
  for the registered manifest feature `feat-library`, which previously had no
  `docs/product-map/` entry. Behaviours verified against `api/routes/library.py`,
  `core/library_sync`, `core/library_service/*` and the library BDD/unit suites.
  Status: covered.
