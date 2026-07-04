---
id: feat-core-api-versioning
prd: 6
delivery-tasks: [task-nv12-api-versioning]
bdd: []
code:
  - backend/src/modulo/api/middleware/deprecation_headers.py
  - backend/src/modulo/api/routes/changelog.py
  - frontend/src/views/ApiChangelogView.vue
  - backend/docs/operations/api-versioning.md

depends-on: []
unit-tests:
  - backend/tests/unit/api/test_deprecation_headers.py
  - backend/tests/unit/api/test_changelog.py
status: partial
---

# API Versioning

URL-path versioning (`/api/v1/`, `/api/v2/`, etc.). Policy document at `backend/docs/operations/api-versioning.md`. Changelog endpoint at `GET /api/v1/changelog`. Deprecation headers via `DeprecationHeaderMiddleware`.

## Behaviours

- [ ] Every route uses a shared API version prefix (`/api/v1/`) — currently hardcoded per-router as `prefix="/api/v1"`
- [ ] Version prefix is configurable (single point of change for `/api/v1/` → `/api/v2/`)
- [ ] New API major version can be added alongside previous version (parallel version routing)
- [x] `DeprecationHeaderMiddleware` adds `Deprecation: true`, `Sunset`, and `Link` headers to deprecated endpoints
- [ ] At least one real endpoint is registered as deprecated via `DeprecationHeaderMiddleware.deprecate()`
- [x] `GET /api/v1/changelog` returns all entries sorted by date descending
- [x] `GET /api/v1/changelog/latest` returns the most recent entry
- [x] Changelog entry includes `version`, `date`, `summary`, `changes`, `deprecations`, `migration_url`
- [x] Frontend `ApiChangelogView.vue` renders changelog entries with version badges and deprecation highlights
- [x] Frontend shows link to migration guide when `migration_url` is present
- [ ] Migration guide exists at `docs/operations/migrations/v1-to-v2.md` (or similar)
- [ ] Deprecated endpoint returns `410 Gone` during grace period (30 days after sunset)
- [ ] Deprecated endpoint is removed entirely after grace period
- [ ] Deprecation period is a minimum 90 days from announcement to sunset
- [ ] At most two major API versions are supported simultaneously
- [ ] Minor version bumps (`v1.0` → `v1.1`) are backward-compatible and do not require a new URL prefix
- [x] Breaking change definition is documented (field removals, type changes, semantic changes, auth changes, endpoint removal, required field additions)
- [x] Adding new fields, new endpoints, bug fixes, performance improvements do NOT require version bump
- [ ] Major version deprecation is announced in the changelog and via admin UI notification
- [x] `/api/v1/changelog` endpoint has unit tests
- [x] `DeprecationHeaderMiddleware` has unit tests
- [ ] BDD feature files exist for versioning/deprecation behaviour

## Known Gaps

- No dedicated PRD section for API versioning — policy lives in `backend/docs/operations/api-versioning.md` only
- `DeprecationHeaderMiddleware.deprecate()` is never called — no real endpoint is registered as deprecated
- No version routing mechanism exists — `/api/v1/` is hardcoded in every router's `APIRouter(prefix="/api/v1/...")`, making parallel version support impossible without significant refactoring
- No migration guides exist at `docs/operations/migrations/`
- Changelog has only a single seed entry (`"1.0"`) — no mechanism for programmatic entry addition
- No BDD feature files for API versioning behaviour
- `410 Gone` grace period behaviour is documented policy but not implemented
- The "at most two major versions supported" policy cannot currently be enforced without a version routing mechanism

## Error Handling

- [x] `GET /api/v1/changelog` returns empty list when no entries exist (no crash)
- [x] `GET /api/v1/changelog/latest` returns 404 when no entries exist
- [x] Frontend shows inline error on fetch failure with retry button
- [x] Frontend shows empty state when no changelog entries exist

## Edge Cases

- [x] Single changelog entry renders correctly
- [ ] No changelog entries — `_SEED_ENTRIES` is statically seeded but has no programmatic API to add entries
- [ ] Changelog entries are lost on server restart (in-memory only)

## Resilience & Integration Robustness

- [x] Middleware prefix matching handles sub-paths correctly
- [x] Unknown path requests get no deprecation headers (no false positives)

## QA History

- 2026-07-05: cross-cutting QA (index 157): Fixed MAJOR — added 5 unit tests for changelog endpoints (list, latest, empty 404, model fields, migration_url). Fixed MAJOR — replaced 2 hardcoded error strings in ApiChangelogView.vue with $t() wrappers, added 2 i18n keys to en-US. Added Error Handling, Edge Cases, Resilience sections to product map. Created website docs stub.