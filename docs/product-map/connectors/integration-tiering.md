---
id: feat-integration-tiering
prd: 8.6
delivery-tasks: []
bdd: [backend/tests/bdd/features/library/tiering.feature]
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
  - backend/tests/unit/crud/test_connector_instance_tier.py
  - backend/tests/unit/crud/test_model_backend_tier.py
  - backend/tests/unit/crud/test_library_primitive_tier.py
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
- [x] `connector_instance.py` CRUD `list_connector_instances` wraps BOTH `total_query` AND `items_stmt` in `try/except ProgrammingError` returning empty PageResult (graceful degradation when table doesn't exist yet)
- [x] `model_backend.py` CRUD `list_model_backends` wraps BOTH `total_query` AND `items_stmt` in `try/except ProgrammingError` returning empty PageResult (graceful degradation when table doesn't exist yet)
- [x] `connectors.py` all 5 routes catch `except HTTPException` (re-raise) and `except Exception` returning 500 with structured logging (consistency with `model_backends.py` pattern)
- [x] `model_backends.py` all 5 routes catch `except HTTPException` (re-raise) and `except Exception` returning 500 with structured logging
- [ ] `library_primitive.py` CRUD `list_library_primitives` catches `SQLAlchemyError` on both count and items queries but does NOT catch `ProgrammingError` separately — the route-level catch handles it, but with less specific messaging

### Edge Cases

- [x] `model_backend.py` route `create_model_backend_endpoint` already validates `tier` via `Literal["native", "preview", "in_dev"]` — Pydantic rejects invalid values with 422 before reaching the handler body
- [x] `ConnectorUpdate` makes `tier` optional (`None = None`) so PATCH without `tier` leaves the existing value unchanged
- [x] `ConnectorResponse.tier` is typed as plain `str` (not `Literal`) — the Pydantic model accepts any string value from the DB, so an invalid DB value (e.g. from a direct SQL insert) would serialize but fail to re-parse if sent back to a Create/Update endpoint
- [ ] `copy_to_adapt` in `library_primitive.py` CRUD does NOT propagate `tier` — copied primitives lose their tier classification and get `native` (server_default). This means a `preview` primitive copied via copy-to-adapt becomes `native` in the target org with no indication of changed status.
- [x] Unit tests (`test_connector_instance_tier.py`, `test_model_backend_tier.py`, `test_library_primitive_tier.py`) and integration tests exercise the `excluded_tiers` parameter of all three list CRUD functions to verify that `in_dev` items are actually excluded from queries

### 2026-07-12 — Round 3 QA (improve-architecture batch 2)

**Fixed (MINOR):** Added `from None` to 3 `except IntegrityError: raise HTTPException(...)` catch blocks in `connectors.py` (list, get, delete endpoints) to fix B904 lint warnings. The `create` and `update` endpoint handlers already had `from None`; these 3 were inconsistent.

**Status:** partial (6 known gaps unchanged).

## QA History

### 2026-07-09 — Cross-cutting QA (improve-architecture index 288)

**Fixed (CRITICAL):** Added `except HTTPException: raise` + `except Exception → 500` with `logger.exception` guards to all 5 connector routes in `connectors.py` (list, create, get, update, delete). Previously only caught `ProgrammingError→501` and `SQLAlchemyError→503`, allowing Python-level errors (TypeError, KeyError, ValueError from model_validate/dict processing) to propagate as opaque 500 to CatchAllMiddleware. `model_backends.py` already had these guards — `connectors.py` was inconsistent.

**Fixed (MAJOR):** Wrapped `items_stmt` in `try/except ProgrammingError` in both `connector_instance.py` and `model_backend.py` CRUD `list_*` functions. Previously only `total_query` had the guard — a ProgrammingError on the items query would propagate unhandled.

**Fixed (MAJOR):** Replaced `String(err)` / `e instanceof Error ? e.message : String(e)` with `formatApiError(err)` in all 3 error handlers (create, update, delete) in `AdminConnectorsView.vue` frontend. API errors from openapi-fetch are objects, not strings — bare `String(err)` produced `[object Object]` on API failures.

**Remaining gaps unchanged:** No API override for in-dev visibility, no tier promotion workflow, copy_to_adapt doesn't propagate tier, no migration rollback test, ConnectorResponse.tier typed as str.

### 2026-07-09 — Cross-cutting QA (improve-architecture index 348)

**Stale claims corrected:** Marked `excluded_tiers` test checkbox `[x]` — tests exist in `test_connector_instance_tier.py`, `test_model_backend_tier.py`, `test_library_primitive_tier.py`. Added these to `unit-tests:` frontmatter. Added `backend/tests/bdd/features/library/tiering.feature` to `bdd:` frontmatter. Updated Known Gaps to reflect that BDD coverage now exists for library primitives (but is still missing for connectors/model backends).

## Known Gaps

- **No API override for In-Dev visibility.** Server-side filtering of `in_dev` items is hardcoded in the CRUD/service layer — there is no query parameter (`?include_in_dev=true`) that an admin could use to reveal In-Dev entries for testing. The frontend has no debug toggle either (ADR 010 open question).
- **No tier promotion/demotion workflow.** Changing a connector/backend/primitive from `preview` to `native` (or vice versa) is a raw `PATCH` with no approval process, audit trail, or changelog entry — flagged as an open question in ADR 010.
- **`copy_to_adapt` does not propagate tier.** The CRUD function creates a copy without setting `tier`, so the copy silently falls back to `"native"` via server_default. A user copying a `preview` primitive gets a `native` copy with no indication that the tier changed.
- **Connector/ModelBackend tiering still lacks BDD coverage.** Only library primitives have BDD scenarios (`tiering.feature` — 2 scenarios covering create-with-tier and default-tier). Connector and ModelBackend tier creation/sorting lacks BDD coverage entirely.
- **No dedicated migration rollback test** — `0062_add_integration_tier` has a `downgrade()` but no automated test exercises drop-and-recreate of the `tier` column/constraint.
- **`ConnectorResponse.tier` is typed as `str` not `Literal`.** An invalid DB value would serialize to JSON correctly but a subsequent PATCH round-trip would fail Pydantic validation with 422. Consider using `Literal` on response models or adding a `@field_validator`.
