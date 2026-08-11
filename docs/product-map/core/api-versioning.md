---
id: feat-core-api-versioning
prd: 6
delivery-tasks: [task-nv12-api-versioning]
bdd: []
code:
  - backend/src/modulo/api/middleware/deprecation_headers.py
  - backend/src/modulo/api/main.py
  - backend/docs/operations/api-versioning.md
  - backend/docs/operations/migrations/v1-config-to-admin.md

depends-on: []
unit-tests:
  - backend/tests/unit/api/test_deprecation_headers.py
status: partial
---

# API Versioning

URL-path versioning (`/api/v1/`, `/api/v2/`, etc.). Policy document at `backend/docs/operations/api-versioning.md`. Deprecation headers via `DeprecationHeaderMiddleware`.

## Behaviours

- [ ] Every route uses a shared API version prefix (`/api/v1/`) — currently hardcoded per-router as `prefix="/api/v1"`
- [ ] Version prefix is configurable (single point of change for `/api/v1/` → `/api/v2/`)
- [ ] New API major version can be added alongside previous version (parallel version routing)
- [x] `DeprecationHeaderMiddleware` adds `Deprecation: true`, `Sunset`, and `Link` headers to deprecated endpoints
- [x] At least one real endpoint is registered as deprecated via `DeprecationHeaderMiddleware.deprecate()`
- [x] Migration guide exists for the deprecated endpoint at `backend/docs/operations/migrations/v1-config-to-admin.md`
- [x] Deprecated endpoint returns `410 Gone` when the sunset date has passed (grace period)
- [ ] Deprecated endpoint is removed entirely after the 30-day grace period
- [ ] Deprecation period is a minimum 90 days from announcement to sunset
- [ ] At most two major API versions are supported simultaneously
- [ ] Minor version bumps (`v1.0` → `v1.1`) are backward-compatible and do not require a new URL prefix
- [x] Breaking change definition is documented (field removals, type changes, semantic changes, auth changes, endpoint removal, required field additions)
- [x] Adding new fields, new endpoints, bug fixes, performance improvements do NOT require version bump
- [ ] Major version deprecation is announced via admin UI notification
- [x] `DeprecationHeaderMiddleware` has unit tests
- [ ] BDD feature files exist for versioning/deprecation behaviour

## Known Gaps

- No dedicated PRD section for API versioning — policy lives in `backend/docs/operations/api-versioning.md` only
- No version routing mechanism exists — `/api/v1/` is hardcoded in every router's `APIRouter(prefix="/api/v1/...")`, making parallel version support impossible without significant refactoring
- Only one migration guide exists (`v1-config-to-admin.md`) — no generic `v1-to-v2.md` pattern established
- No BDD feature files for API versioning behaviour
- The "at most two major versions supported" policy cannot currently be enforced without a version routing mechanism
- `DeprecationHeaderMiddleware.deprecate()` is called for `/api/v1/system-admin/config` with a future sunset date — the 410 Gone logic is tested but no endpoint has actually passed its sunset in production

## Resilience & Integration Robustness

- [x] Middleware prefix matching handles sub-paths correctly
- [x] Unknown path requests get no deprecation headers (no false positives)

## QA History

Note: the API Changelog feature (endpoint and `ApiChangelogView.vue`) referenced below was removed end-to-end in PR #1018. The lines below are a historical record of QA work on the now-removed feature.

- 2026-07-05: cross-cutting QA (index 157): Fixed MAJOR — added 5 unit tests for the now-removed changelog endpoints (list, latest, empty 404, model fields, migration_url). Fixed MAJOR — replaced 2 hardcoded error strings in the now-removed ApiChangelogView.vue with $t() wrappers, added 2 i18n keys to en-US. Added Error Handling, Edge Cases, Resilience sections to product map. Created website docs stub.
- 2026-07-07: cross-cutting QA (index 323): Fixed CRITICAL — added 410 Gone behaviour to DeprecationHeaderMiddleware when sunset date has passed. Fixed MAJOR — registered `/api/v1/system-admin/config` as a deprecated endpoint with future sunset. Fixed MAJOR — added `POST /api/v1/changelog` for programmatic changelog entry creation (6 new unit tests: 4 deprecation/410, 2 changelog POST) — this changelog endpoint was removed in PR #1018. Created migration guide at `backend/docs/operations/migrations/v1-config-to-admin.md`. Updated product map to mark 4 previously-unchecked behaviours as [x].
