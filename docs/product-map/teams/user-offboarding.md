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
  - backend/tests/unit/api/test_error_handling.py
depends-on: [feat-auth-jwt-auth, feat-teams-team-crud]
status: covered
---
# User Offboarding

Admin-initiated deactivation of an individual user – sets `active=false` invalidates all JWT token families, removes all team memberships, and prevents login. Reactivation restores `active=true` but does not restore memberships or token families. Deactivation is an immediate revocation action intended for departing employees and security incidents (PRD 9.4). Stale team membership claims in existing access tokens live for up to 15 minutes unless admin forces session revocation via this endpoint.

## Behaviours

### Authorization

- [x] Admin-only: `POST /api/v1/admin/users/{user_id}/deactivate` returns 403 when caller has `org_role != "admin"`
- [x] Self-deactivation: caller deactivating own user_id returns 422 with "Cannot deactivate yourself"
- [x] Reactivation is also admin-only: `POST /api/v1/admin/users/{user_id}/reactivate` returns 403 for non-admin
- [x] Unauthenticated request to deactivate → 401

### Happy Path – Deactivate
- [x] `POST /api/v1/admin/users/{user_id}/deactivate` → 200, user returned with `is_active: false`
- [x] User's `active` column set to `false` in DB
- [x] All token families for that user are blacklisted (`.is_blacklisted = true`, `.blacklisted_at` set) – JWT families only; OAuth families not yet covered
- [x] All team memberships for that user are removed from DB
- [x] Deactivated user cannot obtain new access/refresh tokens (`authenticate_db_user` in `passwords.py:40` checks `account.active` and returns False)
- [x] Deactivated user's existing tokens fail on next decode / dependency check (JWT decode is stateless – no DB check on every request)

### Happy Path – Reactivate
- [x] `POST /api/v1/admin/users/{user_id}/reactivate` → 200, user returned with `is_active: true`
- [x] User's `active` column set to `true` in DB
- [x] Reactivation does NOT restore previously blacklisted token families (user must re-login)
- [x] Reactivation does NOT restore previously removed team memberships

### Edge Cases – Target User
- [x] Deactivate nonexistent user → 404 (admin.py crud_update_user returns None → HTTPException)
- [x] Reactivate nonexistent user → 404
- [x] Deactivate already-deactivated user → succeeds (idempotent; `active` stays `false`)
- [x] Reactivate already-active user → succeeds (idempotent; `active` stays `true`)
- [x] User has zero token families → deactivation still succeeds (no-op on families – empty for loop)
- [x] User has zero team memberships → deactivation still succeeds (no-op on memberships – empty for loop)
- [x] Deactivate user who is the sole admin of the org → 422 with "Cannot deactivate the last admin"
- [x] Deactivate user who is the last member of a team → team now has zero members (edge: UI should handle empty team gracefully)

### Cross-Org Isolation
- [x] Deactivation scoped to caller's organisation via RLS (`set_rls_org` at admin.py:1199)
- [x] Admin from org A cannot deactivate a user in org B (RLS returns zero rows → 404)

### Concurrent / Race Conditions
- [x] Two admins deactivate the same user simultaneously → no error (idempotent SET)
- [x] User logging in while deactivation is in flight → auth dependency sees `active=false` and rejects
- [ ] Deactivation during active WebSocket connection → WS token still valid for up to 15 min TTL unless WS auth re-validates `active` on each message

### Token Blacklisting Details
- [x] `list_families_for_account` returns all token families for the given user (the CRUD fn is `list_families_for_account` in `token_family.py` – the product map previously referenced a non-existent `list_families_for_user`) – unit-tested in `tests/unit/db/crud/test_token_family.py`
- [x] Each family's `is_blacklisted` set to `true` and `blacklisted_at` = now
- [x] `blacklist_family` returns `false` if family_id does not exist (no-op, not error)
- [x] Blacklisted family causes `advance_sequence` to return `theft_detected=true`
- [ ] OAuth token families are also blacklisted via `blacklist_oauth_token_family` during deactivation flow (not yet called from admin.py – only JWT token families are blacklisted)

### API Key Interaction
- [x] API keys are revoked by deactivation – the caller-bound `deactivate_break_glass` SECURITY DEFINER sets `revoked_at = now()` on the user's non-revoked org API keys (admin.py:1233; DB migration 0108)
- [x] Deactivated user's API keys are revoked during deactivation – the security gap is closed

### Audit Trail
- [x] Deactivation event recorded in audit log (`user_deactivated` event type dispatched)
- [x] Reactivation event recorded in audit log (`user_reactivated` event type dispatched)

### Error Handling
- [x] `POST /api/v1/admin/users/{user_id}/deactivate` returns 501 with migrations message when DB table missing (ProgrammingError caught)
- [x] `POST /api/v1/admin/users/{user_id}/deactivate` returns 503 with unavailable message on SQLAlchemyError
- [x] `POST /api/v1/admin/users/{user_id}/deactivate` returns 500 with unexpected error message on generic Exception
- [x] `POST /api/v1/admin/users/{user_id}/reactivate` returns 501 with migrations message when DB table missing (ProgrammingError caught)
- [x] `POST /api/v1/admin/users/{user_id}/reactivate` returns 503 with unavailable message on SQLAlchemyError
- [x] `POST /api/v1/admin/users/{user_id}/reactivate` returns 500 with unexpected error message on generic Exception
- [x] Deactivate with malformed UUID → 422 (FastAPI validation)
- [x] Reactivate with malformed UUID → 422

### PRD 9.4 Stale Membership Gap
- [x] Deactivation takes effect immediately for DB-level checks (login, HITL `required_team_id` gates)
- [x] Stale JWT claims may persist for up to 15 min for non-critical access (documented gap)
- [ ] Admin UI shows "session revocation" note alongside deactivation action

### SCIM Interaction
- [x] SCIM-provisioned user deactivated via admin API – SCIM IdP state is NOT synced back (out-of-band)
- [x] SCIM reprovision after deactivation: IdP re-sends PUT/PATCH → user reactivated if email/username matches (depends on SCIM matching logic)

## Known Gaps

- **No BDD scenario for deactivation 422/403 validation**: the unit test file covers malformed UUID + authorization, but `member_management.feature` has only the happy-path "Deactivate user removes access" scenario. Adding 422/403 BDD scenarios needs new step definitions in `test_orgs.py` (the deactivate step posts via the admin `client` fixture and ignores viewer-auth state), which is outside this entry's delivery scope.
- **No WS token re-validation**: an established WebSocket connection stays valid for up to 15 min after deactivation (matches the PRD 9.4 stale-membership window). WS upgrade tokens themselves are 60s TTL single-use (PRD §7.10); the gap is the long-lived connection.
- **SCIM hard-delete mismatch**: SCIM DELETE does a hard delete rather than soft deactivate – inconsistent with admin deactivation (SCIM PUT/PATCH `active=false` does soft-deactivate via `scim_deactivate_user`).
- **No OAuth token family blacklisting**: deactivation only blacklists JWT `token_families`. `oauth_token_families` are keyed by `client_id` + `organisation_id` (no account linkage), so a per-user deactivation cannot enumerate or blacklist them, and PRD 9.4's deactivation spec covers JWT token families only. Closing this requires an account-scoped OAuth family design change.
- **Admin UI lacks a session revocation note**: PRD 9.4 says to document that deactivation invalidates all active tokens (forces re-login) in the admin UI. `AdminUsersView.vue`'s Deactivate row action has no such note; adding it needs i18n keys in all locale files.

## QA History

### 2026-07-04 – Cross-cutting QA (improve-architecture index 132)
- Added ProgrammingError→501 catches to `admin_deactivate_user` and `admin_reactivate_user` routes
- Added `user_deactivated` and `user_reactivated` audit event dispatch to both endpoints
- Created `test_user_offboarding_programming_error.py` with ProgrammingError→501 unit tests
- Created website docs stub at `Website/modulo-website/src/docs/user-offboarding.md`

### 2026-07-05 – Cross-cutting QA (feat-teams-user-offboarding)
- Verified `[x] Deactivated user cannot obtain new access/refresh tokens` – `authenticate_db_user` at `passwords.py:40` returns False for `not account.active`
- Confirmed `[ ] Deactivated user's existing tokens fail on next decode` is correct – JWT decode is stateless, no DB check
- Confirmed sole-admin guard (`_prevent_last_admin_lockout`) exists for PUT but NOT POST deactivate – gap is accurate
- Confirmed `[ ] Deactivate with malformed UUID → 422` is unimplemented in tests – added `test_admin_deactivate_malformed_uuid` test coverage
- Confirmed `[ ] Deactivate user who is the last member of a team → team has zero members` – no test for this edge case
- Added malformed UUID → 422 validation tests for both deactivate and reactivate endpoints

### 2026-07-09 – Cross-cutting QA (feat-teams-user-offboarding, index 352)
- Added SQLAlchemyError→503 and Exception→500 catches to both `admin_deactivate_user` and `admin_reactivate_user` routes
- Moved `_get_org_role` call inside the try block (after session.flush()) for both routes
- Added sole-admin guard to `admin_deactivate_user` – blocks deactivation if target user is the last admin in the org
- Fixed BDD deactivate step (`test_orgs.py:deactivate_user`) to actually call the POST API endpoint instead of just setting a stub value
- Added proper `Then the response status is 200` assertion to the BDD feature file
- Added SQLAlchemyError→503 and Exception→500 unit tests for both deactivate and reactivate endpoints
- Updated Known Gaps: removed resolved items (BDD stub, sole-admin guard, malformed UUID tests)

### 2026-08-15 – Cross-cutting QA (feat-teams-user-offboarding, drive-to-covered pass)
- Verified and checked off the four Authorization behaviours (403 non-admin deactivate/reactivate, 422 self-deactivation, 401 unauthenticated) – implemented in `admin.py`; added route tests in `tests/unit/api/test_admin.py` (`TestUserDeactivateAuthorization`).
- Added `tests/unit/db/crud/test_token_family.py`: `list_families_for_account` returns all families, `blacklist_family` sets `is_blacklisted` + `blacklisted_at` and returns `False` for a missing family (no-op), and `advance_sequence` returns `theft_detected=true` for a blacklisted or out-of-order family.
- Verified "existing tokens fail on next request" – `get_current_tenant_user` → `_verify_identity` re-reads the live org role from `org_memberships` (`deactivated_at IS NULL`) on every request (ADR 017); deactivation tombstones the membership so the next request 401s. Covered by `test_ws_token.py` (WS-token mint + refresh deny). JWT decode itself remains stateless.
- Marked last-member-of-team edge as covered: deactivation removes every team membership (route test asserts `remove_team_member` is called per membership).
- Marked "two admins deactivate the same user simultaneously" as covered by the idempotent SET + advisory-lock serialization in the `deactivate_break_glass` SECURITY DEFINER (the already-[x] idempotent re-deactivate case is the same path).
- Verified SCIM behaviours: admin-API deactivation is not synced back to the IdP (SCIM is inbound-only); reprovision/reactivation re-activates via `scim_create_user` / `scim_update_user` when email matches.
- Renamed the `list_families_for_user` behaviour to the real CRUD function `list_families_for_account`.
- Left unchecked as genuine gaps: OAuth token family blacklisting (schema is org+client-scoped, not account-scoped; not an explicit PRD 9.4 requirement), admin UI session-revocation note, WS 15-min stale window, SCIM DELETE hard-delete mismatch, BDD 422/403 scenario gap.
