---
id: feat-teams-user-offboarding
prd: 9.4
delivery-tasks: [task-nv1-user-offboarding]
bdd:
  - backend/tests/bdd/features/orgs/member_management.feature
code:
  - backend/src/modulo/api/routes/admin.py
  - backend/src/modulo/api/routes/teams.py
  - backend/src/modulo/auth/jwt.py
  - backend/src/modulo/db/crud/token_family.py
  - backend/src/modulo/db/crud/team_membership.py
unit-tests:
  - backend/tests/unit/api/test_user_offboarding_programming_error.py
depends-on: [feat-auth-jwt-auth, feat-teams-team-crud]
status: partial
---
# User Offboarding

Admin-initiated deactivation of an individual user — sets `active=false` invalidates all JWT token families, removes all team memberships, and prevents login. Reactivation restores `active=true` but does not restore memberships or token families. Deactivation is an immediate revocation action intended for departing employees and security incidents (PRD 9.4). Stale team membership claims in existing access tokens live for up to 15 minutes unless admin forces session revocation via this endpoint.

## Behaviours

### Authorization

- [ ] Admin-only: `POST /admin/users/{user_id}/deactivate` returns 403 when caller has `org_role != "admin"`
- [ ] Self-deactivation: caller deactivating own user_id returns 422 with "Cannot deactivate yourself"
- [ ] Reactivation is also admin-only: `POST /admin/users/{user_id}/reactivate` returns 403 for non-admin
- [ ] Unauthenticated request to deactivate → 401

### Happy Path — Deactivate
- [x] `POST /admin/users/{user_id}/deactivate` → 200, user returned with `is_active: false`
- [x] User's `active` column set to `false` in DB
- [x] All token families for that user are blacklisted (`.is_blacklisted = true`, `.blacklisted_at` set) — JWT families only; OAuth families not yet covered
- [x] All team memberships for that user are removed from DB
- [ ] Deactivated user cannot obtain new access/refresh tokens (auth flow checks `active`)
- [ ] Deactivated user's existing tokens fail on next decode / dependency check

### Happy Path — Reactivate
- [x] `POST /admin/users/{user_id}/reactivate` → 200, user returned with `is_active: true`
- [x] User's `active` column set to `true` in DB
- [x] Reactivation does NOT restore previously blacklisted token families (user must re-login)
- [x] Reactivation does NOT restore previously removed team memberships

### Edge Cases — Target User
- [x] Deactivate nonexistent user → 404 (admin.py crud_update_user returns None → HTTPException)
- [x] Reactivate nonexistent user → 404
- [x] Deactivate already-deactivated user → succeeds (idempotent; `active` stays `false`)
- [x] Reactivate already-active user → succeeds (idempotent; `active` stays `true`)
- [x] User has zero token families → deactivation still succeeds (no-op on families — empty for loop)
- [x] User has zero team memberships → deactivation still succeeds (no-op on memberships — empty for loop)
- [ ] Deactivate user who is the sole admin of the org → 422 or blocked (policy check — verify if enforced)
- [ ] Deactivate user who is the last member of a team → team now has zero members (edge: UI should handle empty team gracefully)

### Cross-Org Isolation
- [x] Deactivation scoped to caller's organisation via RLS (`set_rls_org` at admin.py:629)
- [x] Admin from org A cannot deactivate a user in org B (RLS returns zero rows → 404)

### Concurrent / Race Conditions
- [ ] Two admins deactivate the same user simultaneously → no error (idempotent SET)
- [ ] User logging in while deactivation is in flight → auth dependency sees `active=false` and rejects
- [ ] Deactivation during active WebSocket connection → WS token still valid for up to 15 min TTL unless WS auth re-validates `active` on each message

### Token Blacklisting Details
- [ ] `list_families_for_user` returns all token families for the given user
- [ ] Each family's `is_blacklisted` set to `true` and `blacklisted_at` = now
- [ ] `blacklist_family` returns `false` if family_id does not exist (no-op, not error)
- [ ] Blacklisted family causes `advance_sequence` to return `theft_detected=true`
- [ ] OAuth token families are also blacklisted via `blacklist_oauth_token_family` during deactivation flow (not yet called from admin.py — only JWT token families are blacklisted)

### API Key Interaction
- [x] API keys are revoked by deactivation — `admin_deactivate_user` calls `revoke_api_key` for all non-revoked keys (admin.py:642-652)
- [x] Deactivated user's API keys are revoked during deactivation — the security gap is closed

### Audit Trail
- [x] Deactivation event recorded in audit log (`user_deactivated` event type dispatched)
- [x] Reactivation event recorded in audit log (`user_reactivated` event type dispatched)

### Error Handling
- [x] `POST /admin/users/{user_id}/deactivate` returns 501 with migrations message when DB table missing (ProgrammingError caught)
- [x] `POST /admin/users/{user_id}/reactivate` returns 501 with migrations message when DB table missing (ProgrammingError caught)
- [ ] Deactivate with malformed UUID → 422 (FastAPI validation)
- [ ] Reactivate with malformed UUID → 422

### PRD 9.4 Stale Membership Gap
- [ ] Deactivation takes effect immediately for DB-level checks (login, HITL `required_team_id` gates)
- [ ] Stale JWT claims may persist for up to 15 min for non-critical access (documented gap)
- [ ] Admin UI shows "session revocation" note alongside deactivation action

### SCIM Interaction
- [ ] SCIM-provisioned user deactivated via admin API — SCIM IdP state is NOT synced back (out-of-band)
- [ ] SCIM reprovision after deactivation: IdP re-sends PUT/PATCH → user reactivated if email/username matches (depends on SCIM matching logic)

## Known Gaps

- **No BDD scenarios**: `backend/tests/bdd/features/orgs/member_management.feature` has one deactivation scenario but no detailed assertions for token blacklisting, team membership removal, or API key revocation
- **API keys now revoked**: `admin_deactivate_user` calls `revoke_api_key` for all non-revoked keys (admin.py:642-652). The security gap is **CLOSED**.
- **Audit events now dispatched**: both `admin_deactivate_user` and `admin_reactivate_user` emit audit events. Gap **CLOSED**.
- **ProgrammingError→501 now caught**: both endpoints return 501 when DB migrations missing. Gap **CLOSED**.
- **No sole-admin guard**: deactivating the last admin in an org is not blocked — could leave org unmanageable
- **No WS token re-validation**: WebSocket connections may stay active for up to 15 min after deactivation
- **SCIM hard-delete mismatch**: SCIM DELETE does a hard delete rather than soft deactivate — inconsistent with admin deactivation
- **No OAuth token family blacklisting**: deactivation flow only blacklists JWT token families, not OAuth families

## QA History

### 2026-07-04 — Cross-cutting QA (improve-architecture index 132)
- Added ProgrammingError→501 catches to `admin_deactivate_user` and `admin_reactivate_user` routes
- Added `user_deactivated` and `user_reactivated` audit event dispatch to both endpoints
- Created `test_user_offboarding_programming_error.py` with ProgrammingError→501 unit tests
- Created website docs stub at `Website/modulo-website/src/docs/user-offboarding.md`
