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

- [ ] `in_dev` items are NOT filtered server-side on any list endpoint — `list_connectors_endpoint`, `list_model_backends_endpoint`, and the library `list_primitives` route all return every tier regardless of caller; only the frontend hides `preview`/`in_dev` rows from view. A user calling the API directly (or reading network responses) can see In-Dev entries and their full config. This is a genuine gap, not just an unverified checkbox.

## Known Gaps

- **No server-side tier filtering.** In-Dev items are visible to any authenticated caller via direct API calls (`GET /api/v1/connectors`, `GET /api/v1/model-backends`, `GET /api/v1/libraries`) even though the UI hides them. There is no automated tier-based access control beyond client-side UI visibility.
- **No tier promotion/demotion workflow.** Changing a connector/backend/primitive from `preview` to `native` (or vice versa) is a raw `PATCH` with no approval process, audit trail, or changelog entry — flagged as an open question in ADR 010.
- **No admin-visible debug toggle for In-Dev items.** ADR 010 leaves this as an open question; currently there is no way for an admin to reveal In-Dev entries even for internal testing — they are fully hidden with no override.
- **No BDD coverage yet.** A parallel effort (branch `test/bdd-tiering-community`) is adding BDD scenarios for tiering; none exist in this worktree at the time of writing.
- **No dedicated migration rollback test** — `0062_add_integration_tier` has a `downgrade()` but no automated test exercises drop-and-recreate of the `tier` column/constraint.
