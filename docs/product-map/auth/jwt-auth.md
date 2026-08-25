---
id: feat-auth-jwt-auth
prd: N/A
adr: []
code:
  - backend/src/modulo/auth/jwt.py
  - backend/src/modulo/auth/api_key.py
  - backend/src/modulo/auth/dependencies.py
  - backend/src/modulo/auth/ws_token.py
unit-tests:
  - backend/tests/unit/auth/test_jwt.py
bdd:
  - backend/tests/bdd/features/auth/sso_oidc.feature
  - backend/tests/bdd/features/auth/sso_saml.feature
depends-on: []
status: covered
---

# JWT Auth

Stateless JWT authentication for the Modulo API: access / refresh token issuance,
principal decoding with tenant context, and purpose-scoped compact tokens
(WebSocket, claim, refresh). Consumed by `feat-teams-org-entity` and every
authenticated API route through `auth/dependencies.py`.

## Behaviours

- [x] `create_access_token` issues a signed JWT carrying user identity, role,
      org context, expiry, and a purpose claim
- [x] `decode_principal` validates signature, expiry, issuer, and purpose; returns
      an `AuthenticatedPrincipal` (with `SystemAdminPrincipal` handling) and raises
      on wrong key, expired token, malformed/empty `sub`, missing account id, or a
      token used outside its allowed purpose
- [x] `none` algorithm is rejected and only allowlisted signing algorithms pass
- [x] Refresh tokens carry a `refresh` purpose and issue a new access token via
      `refresh_access_token`
- [x] WebSocket tokens (`create_ws_token`) are accepted only with `ws` purpose
- [x] Claim tokens (`create_claim_token` / `decode_claim_token`) are purpose-scoped
      short-lived tokens
- [x] Tenant identity is embedded in the token and validated on decode
- [x] Access tokens for the WS purpose and refresh tokens for the WS purpose are
      rejected (purpose isolation)

## Known Gaps

- **No standalone BDD feature file** — JWT behaviour is covered by unit tests only;
  the token flows behind SSO are exercised via the `sso_oidc` / `sso_saml` features.

## QA History

- 2026-08-25: **improve-architecture (product-map walk)** — entry added to close the
  dangling `depends-on: feat-auth-jwt-auth` edge in `teams/org-entity.md`. Behaviours
  re-verified against `auth/jwt.py` and `backend/tests/unit/auth/test_jwt.py`. Status:
  covered.