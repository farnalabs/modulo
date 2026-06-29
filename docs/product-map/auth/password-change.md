---
id: feat-auth-password-change
prd: 9.4
delivery-tasks: []
bdd: backend/tests/bdd/features/auth/change_password.feature
unit-tests:
  - backend/tests/unit/api/test_me_password.py
code:
  - backend/src/modulo/api/routes/me.py
  - frontend/src/views/MyProfileView.vue
  - frontend/src/__tests__/MyProfileView.spec.ts
depends-on:
  - feat-auth-jwt-auth
status: covered
---

# Password Change

Logged-in users can change their own password via the My Profile page. Admins can also change their password through the same UI. Password change invalidates all existing JWT token families, forcing re-login.

## Behaviours

### API — `PUT /api/v1/me/password`

- [x] Accepts `current_password` and `new_password` in JSON body
- [x] Returns 200 with `{"detail": "Password changed successfully"}` on success
- [x] Validates current password against stored bcrypt hash
- [x] Returns 400 if current password is incorrect
- [x] Returns 400 if user has no local password (SSO/OIDC/SAML user)
- [x] Returns 422 if new password fails strength validation
- [x] Returns 422 if new password is too short (< 8 chars)
- [x] Returns 404 if authenticated user is not found in DB
- [x] Returns 403 if request is unauthenticated (no JWT)
- [x] Blacklists all token families for the user after password change
- [x] Hashes new password with bcrypt before storing

### Frontend — My Profile page (`/admin/my-profile`)

- [x] Change Password form with Current Password, New Password, Confirm New Password fields
- [x] Client-side validation: passwords must match
- [x] Client-side validation: new password must be at least 8 characters
- [x] Shows success message on API success
- [x] Shows error message on API failure
- [x] Clears password fields after successful change
- [x] Renders profile info (avatar initial, email, display name, role, member since)
- [x] Role badge displayed next to user info
- [x] Theme settings section (with get/set via `/me/settings`)
- [x] Accessible via sidebar navigation under admin section

### Security

- [x] Token family invalidation on password change (all sessions revoked)
- [x] Current password required to authorize change (prevents hijacked-session abuse)
- [x] Password strength validation (entropy-based rejection of weak passwords)
- [x] SSO users without local password cannot change password via this endpoint
- [ ] Password change logged to audit trail (not yet implemented — audit trail scope gap)

## Known Gaps

- Password change is not logged to the audit trail yet — the audit system currently covers admin actions but not user self-service actions
