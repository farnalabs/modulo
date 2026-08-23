---
id: feat-core-viewmodel-current
prd: 6
delivery-tasks: [task-prd-viewmodel-current-endpoint]
code:
  - backend/src/modulo/api/routes/viewmodel.py
bdd:
  - backend/tests/bdd/features/viewmodel/viewmodel_current.feature
depends-on: [feat-auth-jwt-auth]
unit-tests:
  - backend/tests/unit/api/test_viewmodel_endpoint.py
  - backend/tests/unit/api/test_viewmodel_view.py
  - backend/tests/unit/api/test_viewmodel_error_paths.py
status: covered
---

# ViewModel Current Endpoint

Aggregate endpoint returning the current user's full view of the system — org context, permissions, feature flags, plan context, team memberships, preferences, pipelines, runs, HITL gates, and saved views.

## Behaviours

- [x] Returns user info (username, org, org_role)
- [x] Returns org context (name, settings)
- [x] Returns team memberships with roles
- [x] Returns user preferences
- [x] Returns feature flags with active status per plan tier
- [x] Returns plan info with tier and daily spend limit
- [x] Returns pipelines list and total count
- [x] Returns recent runs list and total count
- [x] Returns pending HITL gates
- [x] Returns all saved views
- [x] Returns current view when `current_view_id` specified
- [x] Supports `view_as_team` for admin users
- [x] Admin-only `view_as_team` enforcement (403 for non-admin)
- [x] 401 for unauthenticated requests
- [x] 404 for missing team, org, or user

## Error Handling

- [x] 501 ProgrammingError when DB table missing (viewmodel_current)
- [x] 501 ProgrammingError when DB table missing (viewmodel_list_views)
- [x] 501 ProgrammingError when DB table missing (view_as_team team query) — covered by outer try/except in viewmodel_current()
- [x] 501 ProgrammingError when DB table missing (resolve_plan_context) — covered by outer try/except in viewmodel_current()

## Known Gaps

- Plan limits are basic (daily_spend_limit only)
- No Redis/response caching layer
- No pagination on team memberships (truncated flag is always false)
- ~~No dedicated viewmodel BDD feature file~~ — RESOLVED 2026-08-23: added `backend/tests/bdd/features/viewmodel/viewmodel_current.feature` (16 scenarios) with co-located step definitions driving the real `/api/v1/viewmodel/current` route against mocked CRUD deps — covers user identity/org context, memberships, preferences, feature flags, plan info, pipelines+runs, pending HITL gates, saved/current views, view_as_team 403/404, unauthenticated 401, missing org/account 404, ProgrammingError→501, SQLAlchemyError→503. Status: partial → covered.
- `/api/v1/license` route does not query DB, so no ProgrammingError catch needed (its `except Exception` is overly broad but harmless)


## QA History

### 2026-08-23 — improve-architecture (feat-core-viewmodel-current)
- Closed the "No dedicated viewmodel BDD feature file" gap: added `viewmodel_current.feature` + `test_viewmodel_current_steps.py` and linked them in the `bdd:` field; `status: partial` → `covered`.
- The 16 scenarios exercise the real route: aggregate identity/org, team memberships+preferences, feature flags, plan tier+spend limit, pipelines+runs totals, pending HITL gates, saved views + selected current view, admin-only view_as_team (403 non-admin / 404 unknown team), unauthenticated 401, missing org/account 404, and ProgrammingError→501 / SQLAlchemyError→503 error mapping.
- view_as_team behaviour overlaps the existing `teams/view_as_team.feature`; the new feature adds the missing 403/404 admin-gate coverage for the aggregate endpoint itself.

### 2026-08-11 — improve-architecture
- Corrected `status: covered` → `partial`. This entry has no dedicated BDD feature file (`bdd: []`) and relies on indirect `view_as_team` BDD + unit tests, so `covered` (all behaviours BDD-tested) was inaccurate.

### 2026-07-05 — Cross-cutting QA pass (part 2)
- Verified all 16 behaviours against code: all implemented and tested
- Confirmed items #42 and #43 (ProgrammingError for team query and resolve_plan_context) are covered by the outer try/except in `viewmodel_current()` — marked `[x]`
- Fixed stale Known Gap: `/api/v1/license` doesn't query DB, so no ProgrammingError catch needed
- Added ProgrammingError→501 test for `resolve_plan_context` path
- Added ProgrammingError→501 test verifying catch block works with `view_as_team` query param
- Fixed pre-existing test bug: mock views in `test_viewmodel_view.py` missing `account_id` attribute, causing `ViewInfo.model_validate` failure with `ValidationError`
- Created website docs stub at `Website/modulo-website/src/docs/viewmodel.md`

### 2026-07-12 — Round 3 QA (improve-architecture batch 3)
- **Fixed (MINOR):** Added `from None` to 10 `except` blocks missing explicit exception chaining across `license_info`, `me`, `viewmodel_current`, and `viewmodel_list_views` handlers. B904 requires explicit `raise ... from err` or `raise ... from None` within except blocks.

### 2026-07-03 — Cross-cutting QA pass
- Added `try/except ProgrammingError` → 501 to `viewmodel_current()` and `viewmodel_list_views()` in backend/src/modulo/api/routes/viewmodel.py
- Created `backend/tests/unit/api/test_viewmodel_error_paths.py` with 7 error-path tests:
  - 5 404/400 tests for missing org, account, team, no-org states
  - 2 ProgrammingError→501 tests for both routes
