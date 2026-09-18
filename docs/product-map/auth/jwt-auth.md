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
  - backend/tests/bdd/features/auth/jwt_auth_crypto.feature
  - backend/tests/bdd/features/auth/jwt_security.feature
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

None acknowledged: the token-utility contract (mint/decode, purpose isolation,
rotation propagation, claim-token scoping) is now locked by the executing
`jwt_auth_crypto.feature` BDD surface (added 2026-09-18) driving the real
`modulo.auth.jwt` seams, and the API-level login/refresh/logout/tamper flows are
covered by `jwt_security.feature`.

## QA History

- 2026-09-18: **improve-architecture (product-map walk)** — closed the
  "No standalone BDD feature file" gap. Registered
  `auth/jwt_auth_crypto.feature` into the executing BDD suite from the new
  `steps/test_jwt_auth_crypto.py`, driving the real `modulo.auth.jwt` seams
  network-free and DB-free: access-token mint/decode round-trip (identity, role,
  tenant org, `client_kind`), wrong-secret / tampered-signature / expired /
  `alg=none` / missing-subject rejections, the purpose-isolation matrix (access /
  `ws` / `refresh` accepted only under their own purpose, the `refresh_access_token`
  rotation seam refusing a `ws` token), refresh-rotation propagation of identity +
  credential class into the new access token, `decode_claim_token` HITL-gate
  run/gate scoping with a wrong-gate refusal, and the legacy no-`client_kind`
  token decoding as `browser`. `_ORPHANED_BDD_FEATURES` stays empty.
- 2026-08-25: **improve-architecture (product-map walk)** — entry added to close the
  dangling `depends-on: feat-auth-jwt-auth` edge in `teams/org-entity.md`. Behaviours
  re-verified against `auth/jwt.py` and `backend/tests/unit/auth/test_jwt.py`. Status:
  covered.
