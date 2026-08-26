---
id: feat-core-oidc-integration
prd: 9.4
adr: []
code:
  - backend/src/modulo/auth/oidc_verify.py
  - backend/src/modulo/auth/sso.py
unit-tests:
  - backend/tests/unit/auth/test_oidc_verify.py
bdd:
  - backend/tests/bdd/features/auth/sso_oidc.feature
  - backend/tests/bdd/steps/test_sso_oidc.py
depends-on:
  - feat-auth-jwt-auth
status: covered
---

# OIDC Integration

OpenID Connect upstream integration: discovery-document fetch, JWKS retrieval and
caching, and signature verification of the provider-issued `id_token`, wired into
the SSO callback flow (`feat-auth-sso-provider-ui`).

## Behaviours

- [x] Provider `jwks_uri` is discovered from the OpenID Connect discovery document
- [x] JWKS is fetched over HTTPS and cached in-memory with a 1-hour TTL
- [x] `id_token` JWT signature is verified against the matching JWK
- [x] `iss` must match the provider issuer; `aud` must match the client id; `exp`
      must not be expired
- [x] `alg` is restricted to an allowlist (`none` is rejected); key-type / algorithm
      mismatches fail closed
- [x] OIDC SSO callback exchanges the authorization code and issues a JWT pair
      (`feat-auth-jwt-auth`)

## Known Gaps

- **JWKS cache is in-memory per process** — no shared/distributed cache across
  workers; a rotated key set resolves within one TTL per worker.

## QA History

- 2026-08-25: **improve-architecture (product-map walk)** — entry added to close the
  dangling `depends-on: feat-core-oidc-integration` edge in `auth/sso-provider-ui.md`.
  Behaviours re-verified against `auth/oidc_verify.py`, `sso_oidc.feature`, and unit
  tests. Status: covered.
