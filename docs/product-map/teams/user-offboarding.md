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
  - backend/tests/bdd/features/orgs/member_management.feature
depends-on: [feat-auth-jwt, feat-teams-team-crud]
status: partial
---

# User Offboarding

Admin-initiated deactivation of an individual user — sets `active=false`, 
invalidates all JWT token families, removes all team memberships, and 
prevents login. Reactivation restores `active=true` but does not restore 
memberships or token families.

Deactivation is an immediate revocation action intended for departing 
employees and security incidents (PRD §9.4). Stale team membership 
claims in existing access tokens live for up to 15 minutes unless 
admin forces session revocation via this endpoint.

## Behaviours

### Authorization
- [ ] Admin-only: `POST /admin/users/{user_id}/deactivate` returns 403 when caller has `org_role != "admin"`
- [ ] Self-deactivation: caller deactivating own user_id returns 422 with "Cannot deactivate yourself"
- [ ] Reactivation is also admin-only: `POST /admin/users/{user_id}/reactivate` returns 403 for non-admin
- [ ] Unauthenticated request to deactivate → 401

### Happy Path — Deactivate
- [ ] `POST /admin/users/{user_id}/deactivate` → 200, user returned with `is_active: false`
- [ ] User's `active` column set to `false` in DB
- [ ] All token families for that user are blacklisted (`.is_blacklisted = true`, `.blacklisted_at` set)
- [ ] All team memberships for that user are removed from DB
- [ ] Deactivated user cannot obtain new access/refresh tokens (auth flow checks `active`)
- [ ] Deactivated user's existing tokens fail on next decode / dependency check

### Happy Path — Reactivate
- [ ] `POST /admin/users/{user_id}/reactivate` → 200, user returned with `is_active: true`
- [ ] User's `active` column set to `true` in DB
- [ ] Reactivation does NOT restore previously blacklisted token families (user must re-login)
- [ ] Reactivation does NOT restore previously removed team memberships

### Edge Cases — Target User
- [ ] Deactivate nonexistent user → 404
- [ ] Reactivate nonexistent user → 404
- [ ] Deactivate already-deactivated user → succeeds (idempotent; `active` stays `false`)
- [ ] Reactivate already-active user → succeeds (idempotent; `active` stays `true`)
- [ ] User has zero token families → deactivation still succeeds (no-op on families)
- [ ] User has zero team memberships → deactivation still succeeds (no-op on memberships)
- [ ] Deactivate user who is the sole admin of the org → 422 or blocked (policy check — verify if enforced)
- [ ] Deactivate user who is the last member of a team → team now has zero members (edge: UI should handle empty team gracefully)

### Cross-Org Isolation
- [ ] Deactivation scoped to caller's organisation via RLS (`set_rls_org`)
- [ ] Admin from org A cannot deactivate a user in org B (RLS returns zero rows → 404)

### Concurrent / Race Conditions
- [ ] Two admins deactivate the same user simultaneously → no error (idempotent SET)
- [ ] User logging in while deactivation is in flight → auth dependency sees `active=false` and rejects
- [ ] Deactivation during active WebSocket connection → WS token still valid for up to 15 min TTL unless WS auth re-validates `active` on each message

### Token Blacklisting Details
- [ ] `list_families_for_user` returns all token families for the given user
- [ ] Each family's `is_blacklisted` set to `true` and `blacklisted_at` = now
- [ ] `blacklist_family` returns `false` if family_id does not exist (no-op, not error)
- [ ] Blacklisted family causes `advance_sequence` to return `theft_detected=true`
- [ ] OAuth token families are also blacklisted via `blacklist_oauth_token_family` during deactivation flow (check: is this called from admin.py?)

### API Key Interaction
- [ ] API keys are NOT revoked by deactivation (check: admin.py does not call `revoke_api_key`)
- [ ] Deactivated user's API keys still work until explicitly revoked (potential gap — document or fix)

### Audit Trail
- [ ] Deactivation event recorded in audit log (check: does admin.py emit an audit event?)
- [ ] Reactivation event recorded in audit log

### PRD §9.4 Stale Membership Gap
- [ ] Deactivation takes effect immediately for DB-level checks (login, HITL `required_team_id` gates)
- [ ] Stale JWT claims may persist for up to 15 min for non-critical access (documented gap)
- [ ] Admin UI shows "session revocation" note alongside deactivation action

### SCIM Interaction
- [ ] SCIM-provisioned user deactivated via admin API — SCIM IdP state is NOT synced back (out-of-band)
- [ ] SCIM reprovision after deactivation: IdP re-sends PUT/PATCH → user reactivated if email/username matches (depends on SCIM matching logic)

## Known Gaps

- **No BDD scenarios**: `backend/tests/bdd/features/orgs/member_management.feature` is a placeholder with zero real scenarios
- **No unit tests**: no dedicated unit tests for the deactivation/reactivation endpoints
- **API keys not revoked**: deactivation does not call `revoke_api_key`; deactivated user's API keys remain valid — a security gap for departing employees
- **No audit events**: `admin_deactivate_user` does not write to the audit log (check CRUD wrapper)
- **No sole-admin guard**: deactivating the last admin in an org is not blocked — could leave org unmanageable
- **No WS token re-validation**: WebSocket connections may stay active for up to 15 min after deactivation
- **SCIM hard-delete mismatch**: SCIM DELETE does a hard delete rather than soft deactivate — inconsistent with admin deactivation
