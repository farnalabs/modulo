---
id: feat-integration-tiering
prd: 15
delivery-tasks: []
bdd: []
code:
  - backend/src/modulo/db/migrations/versions/0062_add_integration_tier.py
  - backend/src/modulo/db/models/connector_instance.py
  - backend/src/modulo/db/models/model_backend.py
  - backend/src/modulo/db/models/library_primitive.py
  - backend/src/modulo/db/crud/connector_instance.py
  - backend/src/modulo/db/crud/model_backend.py
  - backend/src/modulo/db/crud/library_primitive.py
  - backend/src/modulo/api/routes/connectors.py
  - backend/src/modulo/api/routes/model_backends.py
  - backend/src/modulo/api/routes/library.py
  - backend/src/modulo/core/library_service/__init__.py
  - frontend/src/views/AdminConnectorsView.vue
  - frontend/src/views/AdminModelBackendsView.vue
  - frontend/src/views/LibraryView.vue
unit-tests:
  - backend/tests/unit/api/test_api_contract.py
  - backend/tests/unit/api/test_connectors_endpoint.py
  - backend/tests/unit/api/test_model_backends_endpoint.py
  - frontend/src/__tests__/AdminConnectorsView.spec.ts
  - frontend/src/__tests__/AdminModelBackendsView.spec.ts
  - frontend/src/__tests__/LibraryView.spec.ts
depends-on: [feat-connectors-hub, feat-model-backends-hub, feat-pipelines-library]
status: partial
---

# Integration Tier Classification (Native / Preview / In-Dev)

Every connector, model backend, and library primitive carries a `tier` field
(`native` | `preview` | `in_dev`) signalling maturity. Native items are
surfaced prominently; Preview items are segregated behind a collapsed
disclosure; In-Dev items are hidden from the UI entirely. See ADR 010.

## Behaviours

### Schema & migration

- [x] `tier` column (String(20), NOT NULL, `server_default="native"`) added to `connector_instances`, `model_backends`, and `library_primitives` via migration `0062_add_integration_tier`
- [x] CHECK constraint `ck_<table>_tier` restricts values to `native`, `preview`, `in_dev` on all three tables
- [x] Existing rows default to `native` on migration (server_default), so no pre-existing connector/backend/workflow silently disappears from the UI

### API — request/response models

- [x] `ConnectorCreate`/`ConnectorResponse` (`connectors.py`) include `tier: Literal["native","preview","in_dev"]`, defaulting to `"native"` on create
- [x] `ConnectorUpdate` accepts optional `tier` for changing classification
- [x] `ModelBackendCreate`/`ModelBackendResponse`/`ModelBackendUpdate` mirror the same `tier` contract
- [x] `LibraryPrimitiveCreate`/`LibraryPrimitiveResponse`/`LibraryPrimitiveUpdate` mirror the same `tier` contract
- [x] Contract tests (`test_api_contract.py`) assert `tier` is present and typed on connector responses

### Frontend — tier-aware rendering

- [x] `AdminConnectorsView.vue`: `nativeConnectors` computed filters out `preview`/`in_dev`; treated as the primary/default list
- [x] `AdminConnectorsView.vue`: `previewConnectors` computed (`tier === 'preview'`) rendered inside a collapsed `<details data-testid="connectors-preview-section">` only when non-empty
- [x] `AdminModelBackendsView.vue` follows the identical native/preview split pattern
- [x] `LibraryView.vue` Native section: `nativePrimitives` computed excludes `preview`/`in_dev`; `previewPrimitives` shown in a collapsed `<details data-testid="library-preview-section">`
- [x] In-Dev (`tier === 'in_dev'`) items are excluded from every rendered list on all three views — no admin toggle exists to reveal them
- [x] A connector/backend/primitive without a `tier` value (legacy data) defaults to `native` treatment client-side (`?? 'native'`)

### Server-side enforcement

- [x] `in_dev` items ARE filtered server-side: `list_connector_instances` and `list_model_backends` CRUD both default to `excluded_tiers=["in_dev"]` and exclude `in_dev` rows from total/item queries. The library `list_primitives` service applies the same exclusion to org, modulo, and community items. Every list endpoint silently filters `in_dev` via the CRUD/service layer — no caller override is exposed through the API.
- [ ] No API query parameter exists for admins to bypass the `in_dev` exclusion — any operator who needs to see In-Dev items must query the DB directly.

### Error Handling

- [x] All three route files (`connectors.py`, `model_backends.py`, `library.py`) wrap every DB operation in `try/except ProgrammingError` returning 501 Not Implemented with a migration hint
- [x] All three route files also wrap in `except SQLAlchemyError` returning 503 Service Unavailable with a descriptive message
- [x] The `create_connector_endpoint` has a separate `ProgrammingError` catch for the DB insert, distinct from the GitHub scope verification which raises 422
- [x] `list_library_primitives_endpoint` has nested `try/except` blocks: the outer block catches `HTTPException` (re-raises) and generic `Exception` (500), while inner blocks catch `ProgrammingError` (501) and `model_validate` failures (500 with schema-sync message)
- [x] `model_backends.py` CRUD `list_model_backends` wraps the `total_query` in `try/except ProgrammingError` returning an empty `PageResult` (graceful degradation when table doesn't exist yet)
- [ ] `library_primitive.py` CRUD `list_library_primitives` catches `SQLAlchemyError` on both count and items queries but does NOT catch `ProgrammingError` separately — the route-level catch handles it, but with less specific messaging

### Edge Cases

- [x] `model_backend.py` route `create_model_backend_endpoint` already validates `tier` via `Literal["native", "preview", "in_dev"]` — Pydantic rejects invalid values with 422 before reaching the handler body
- [x] `ConnectorUpdate` makes `tier` optional (`None = None`) so PATCH without `tier` leaves the existing value unchanged
- [x] `ConnectorResponse.tier` is typed as plain `str` (not `Literal`) — the Pydantic model accepts any string value from the DB, so an invalid DB value (e.g. from a direct SQL insert) would serialize but fail to re-parse if sent back to a Create/Update endpoint
- [ ] `copy_to_adapt` in `library_primitive.py` CRUD does NOT propagate `tier` — copied primitives lose their tier classification and get `native` (server_default). This means a `preview` primitive copied via copy-to-adapt becomes `native` in the target org with no indication of changed status.
- [ ] No test exercises the `excluded_tiers` parameter of `list_connector_instances`, `list_model_backends`, or `list_library_primitives` to verify that `in_dev` items are actually excluded from queries

## Known Gaps

- **No API override for In-Dev visibility.** Server-side filtering of `in_dev` items is hardcoded in the CRUD/service layer — there is no query parameter (`?include_in_dev=true`) that an admin could use to reveal In-Dev entries for testing. The frontend has no debug toggle either (ADR 010 open question).
- **No tier promotion/demotion workflow.** Changing a connector/backend/primitive from `preview` to `native` (or vice versa) is a raw `PATCH` with no approval process, audit trail, or changelog entry — flagged as an open question in ADR 010.
- **`copy_to_adapt` does not propagate tier.** The CRUD function creates a copy without setting `tier`, so the copy silently falls back to `"native"` via server_default. A user copying a `preview` primitive gets a `native` copy with no indication that the tier changed.
- **No BDD coverage yet.** A parallel effort (branch `test/bdd-tiering-community`) is adding BDD scenarios for tiering; none exist in this worktree at the time of writing.
- **No dedicated migration rollback test** — `0062_add_integration_tier` has a `downgrade()` but no automated test exercises drop-and-recreate of the `tier` column/constraint.
- **`ConnectorResponse.tier` is typed as `str` not `Literal`.** An invalid DB value would serialize to JSON correctly but a subsequent PATCH round-trip would fail Pydantic validation with 422. Consider using `Literal` on response models or adding a `@field_validator`.
