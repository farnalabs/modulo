---
id: feat-core-viewmodel-current
prd: §6
delivery-tasks: [task-prd-viewmodel-current-endpoint]
code:
  - backend/src/modulo/api/routes/viewmodel.py
depends-on: [feat-auth-jwt-auth]
unit-tests: []
status: partial
bdd:
  - backend/tests/unit/api/test_viewmodel_endpoint.py
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

## Known Gaps

- Plan limits are basic (daily_spend_limit only)
- No Redis/response caching layer
- No pagination on team memberships (truncated flag is always false)
