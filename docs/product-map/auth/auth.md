---
id: feat-auth
prd: N/A
adr: []
code:
  - backend/src/modulo/api/routes/auth.py
  - backend/src/modulo/api/routes/sso.py
  - frontend/src/views/LoginView.vue
  - frontend/src/views/AcceptInviteView.vue
  - frontend/src/views/ForceChangePasswordView.vue
  - frontend/src/views/MyProfileView.vue
  - frontend/src/views/OAuthConsentView.vue
unit-tests:
  - backend/tests/unit/api/test_auth_routes_coverage.py
  - backend/tests/unit/auth/test_login_endpoint.py
  - backend/tests/unit/api/test_auth_rate_limiter.py
  - backend/tests/unit/rate_limiter/test_auth_rate_limiter.py
bdd:
  - backend/tests/bdd/features/auth/login.feature
  - backend/tests/bdd/features/auth/jwt_security.feature
  - backend/tests/bdd/features/auth/rbac.feature
  - backend/tests/bdd/features/auth/api_keys.feature
  - backend/tests/bdd/features/auth/tenant_isolation.feature
  - backend/tests/bdd/features/auth/change_password.feature
  - backend/tests/bdd/features/auth/sso_oidc.feature
  - backend/tests/bdd/features/auth/sso_saml.feature
  - backend/tests/bdd/features/auth/sso_team_mapping.feature
depends-on: []
status: covered
---

# Auth

OAuth authorization, sessions, user profile, and brute-force rate limiting. Covers
JWT login (email/password), demo auto-login, invitation enrollment, token rotation,
logout, profile management, password change, and the OAuth consent screen for MCP
clients.

## Behaviours

- [x] Login authenticates credentials, resolves org membership, and mints JWT
      access+refresh token pairs via token families; break-glass credentials are
      consumed with compare-and-swap; brute-force rate limiting applies exponential
      backoff per IP (`backend/src/modulo/api/routes/auth.py`,
      `backend/tests/unit/api/test_auth_routes_coverage.py`)
- [x] Demo auto-login mints short-lived tokens (2h TTL, 4h hard cap) with no
      refresh token; all failures return 404 to hide the feature
- [x] Token rotation validates refresh claims, detects token theft (reused sequence
      numbers blacklist the family), and returns new access+refresh tokens
- [x] Logout blacklists the token family so all tokens from that session are
      invalidated
- [x] `/me` returns the authenticated user's profile with `must_change_password`
      flag for forced password change on first login
- [x] Invitation enrollment handles four account-resolution branches: new account,
      SSO rejection (409), local account without password adopts password, local
      account with password reports existing_account=true
- [x] Password change clears `must_change_password`, invalidates refresh-token
      families, and records an audit event
- [x] OAuth consent screen authorizes third-party MCP applications via PKCE flow
- [x] All auth endpoints are org-RLS scoped; tenant isolation prevents cross-org
      pipeline access (`backend/tests/bdd/features/auth/tenant_isolation.feature`)
- [x] RBAC privilege-cap model enforces team role ≤ org role; team CRUD,
      membership management, and feature gating by role
      (`backend/tests/bdd/features/auth/rbac.feature`)

## Known Gaps

- No dedicated BDD for `/me` password-change forced flow; coverage is via unit
  tests and the `change_password.feature` BDD.

## QA History

- 2026-09-11: **improve-architecture (product-map walk)** — extended the reverse
  testid-coverage guard (`test_mapped_route_elements_cover_owning_view_testids`) to
  `/admin/my-profile`: the whole-page view(s) `MyProfileView.vue` render static `data-testid`s that the
  product map `elements:` inventory already documents, but the surface was not yet
  guarded against drift. The route now maps to its owning view so a newly shipped
  testid can no longer silently stay invisible to Remy's docs indexer /
  `/api/v1/manifest`.

- 2026-09-07: **improve-architecture (product-map walk)** — added this
  behaviour-tracker for `feat-auth`, which previously had behaviours only in
  `manifest.yaml` inline. Behaviours verified against `routes/auth.py`,
  `routes/sso.py`, the auth unit+BDD suites, and frontend views. Status: covered.
