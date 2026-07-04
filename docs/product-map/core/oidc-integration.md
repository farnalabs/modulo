---
id: feat-core-oidc-integration
prd: 9.4, 6.2, 9.2
delivery-tasks: [task-nv6-oidc-integration]
bdd:
  - backend/tests/bdd/features/auth/sso_oidc.feature
depends-on: [feat-core-saml-integration, feat-auth-sso-provider-ui, feat-auth-team-rbac]
code:
  - backend/src/modulo/auth/sso.py
  - backend/src/modulo/auth/oidc_verify.py
  - backend/src/modulo/api/routes/sso.py
  - backend/src/modulo/api/routes/admin_sso.py
  - backend/src/modulo/settings.py
  - backend/src/modulo/api/main.py
unit-tests:
  - backend/tests/unit/auth/test_sso.py
  - backend/tests/unit/auth/test_oidc_verify.py
  - backend/tests/unit/auth/test_sso_oidc_bdd.py
  - backend/tests/unit/auth/test_sso_team_mapping_bdd.py

status: partial
---

# OIDC Integration — OpenID Connect SSO with authorization code flow, discovery document parsing, JIT user provisioning, and group-to-team mapping.

## Behaviours

### Provider configuration (env-var seeding)

- [x] One-time migration from `MODULO_OIDC_PROVIDERS` env var to `sso_providers` DB table on startup — `main.py:_seed_sso_providers`
- [x] Migration skips when env var is empty or `[]`
- [x] Migration skips when providers already exist in DB
- [x] Each env var entry must have `provider_id`, `client_id`, `client_secret`, `discovery_url` — entries missing fields are skipped with warning
- [x] Seeded providers get scopes `["openid", "profile", "email"]` and default role from `modulo_sso_default_role`

### SSO providers endpoint

- [x] `GET /api/v1/auth/sso/providers` returns list of configured OIDC providers with `provider_id`
- [x] Returns `saml: bool` alongside OIDC list
- [x] SAML is reported as enabled only when license present + SAML configured

### Authorization redirect

- [x] `GET /api/v1/auth/oidc/{provider}/login` returns HTTP 307 redirect to IdP authorization endpoint
- [x] Redirect URL includes `client_id`, `response_type=code`, `scope=openid email profile`, `redirect_uri`, signed `state`
- [x] State is HMAC-SHA256 signed for CSRF protection
- [x] Redirect URI derived from `MODULO_PUBLIC_URL`
- [x] Returns 400 with error detail when provider ID is unknown
- [x] Returns error when discovery document lacks `authorization_endpoint`
- [x] Fetches discovery document from provider's `discovery_url` via HTTPS

### Callback / code exchange

- [x] `GET /api/v1/auth/oidc/{provider}/callback` accepts `code` and `state` query params
- [x] Returns 400 when `code` or `state` is missing
- [x] Returns 401 when state signature verification fails (tampered/CSRF)
- [x] Returns 401 when provider ID in state does not match any configured provider
- [x] Exchanges authorization code at IdP token endpoint for `id_token`
- [x] Token endpoint URL fetched from discovery document
- [x] Token exchange uses `grant_type=authorization_code` with client credentials

### ID token processing

- [x] Decodes ID token JWT payload (base64, no signature verification) when no JWKS endpoint available
- [x] Full JWT signature verification when JWKS URI and issuer are present in discovery document — `oidc_verify.py:verify_id_token`
- [x] JWKS in-memory cache with 1-hour TTL
- [x] JWKS cache cleared and retry on verification failure (key rotation support)
- [x] Extracts `email`, `name`/`preferred_username`, `sub` from claims
- [x] Falls back to `sub` claim as email when email is missing
- [x] Falls back to `preferred_username` then email prefix as display name

### JIT user provisioning

- [x] Existing user matched by email gets `sso_subject` and `auth_provider` updated to `oidc`
- [x] New user created with `auth_provider='oidc'`, `password_hash=None`
- [x] New user placed in first org (by `created_at`) or custom `default_org_id`
- [x] `sso_subject` format: `{provider_id}:{sub}`
- [x] Default org role from `modulo_sso_default_role` applied
- [x] Raises RuntimeError when no organisation exists
- [x] Custom `default_org_id` bypasses org lookup

### Group-to-team mapping

- [x] Extracts `groups` claim from ID token
- [x] Looks up DB provider by `client_id` for group mapping configuration
- [x] Applies group mappings: matching groups create or update team membership
- [x] Non-matching groups silently skipped
- [x] Existing membership with different role is updated
- [x] Duplicate membership is not re-added
- [x] Skip group mapping when no groups in ID token
- [x] Skip group mapping when no group mappings configured on provider

### Token issuance

- [x] Issues JWT access token (15-min TTL) on successful OIDC login
- [x] Issues JWT refresh token (7-day TTL) with token family rotation
- [x] Tokens carry `org_role` claim
- [x] Redirects browser to frontend callback URL with `access_token` and `refresh_token`
- [x] Frontend base URL derived from first `CORS_ORIGINS` origin

### SSO provider admin CRUD

- [x] Admin can create OIDC provider with `discovery_url`, `client_id`, `client_secret`
- [x] Admin can update OIDC provider fields
- [x] Admin can delete OIDC provider
- [x] Admin can toggle provider enabled/disabled
- [x] Provider type is validated (must be `oidc` or `saml`)
- [x] Default role is validated (must be `operator` or `runner`)
- [x] Provider scopes stored as JSON text column
- [x] Non-admin users get 403 on all admin SSO endpoints
- [x] `GET /api/v1/admin/sso/providers` lists all SSO providers

### Connection testing

- [x] Admin can test OIDC provider connection
- [x] Test fetches discovery document from provider URL
- [x] Test validates presence of `authorization_endpoint` in discovery document
- [x] Test reports issuer, auth/token/userinfo/JWKS endpoints, supported scopes
- [x] Test returns failure details on network error or invalid document
- [x] Client ID is included in test results for validation info when present

### Group mapping admin

- [x] Admin can set group mappings per provider: `PUT /api/v1/admin/sso/providers/{id}/group-mappings`
- [x] Admin can read group mappings per provider: `GET /api/v1/admin/sso/providers/{id}/group-mappings`
- [x] Group mapping format: `{idp_group, team_id, team_role}`

### Enterprise feature gating

- [x] SSO is flagged as enterprise-tier feature through `require_feature("sso")` dependency
- [x] OIDC login/callback does not require license key (unlike SAML)
- [x] Admin SSO endpoints all require admin `org_role`

## Error Handling

### Discovery fetch errors

- [x] HTTP errors during discovery fetch in `oidc_get_authorize_url` raise ValueError → HTTP 400
- [x] HTTP errors during discovery fetch in `oidc_process_callback` raise ValueError → HTTP 401
- [x] Connection/network errors during discovery fetch raise ValueError with descriptive message
- [x] Missing `authorization_endpoint` in login raises ValueError → HTTP 400
- [x] Missing `token_endpoint` in callback raises ValueError → HTTP 401

### Code exchange errors

- [x] HTTP errors during token exchange raise ValueError → HTTP 401

### ID token verification errors

- [x] Missing `jwks_uri`/`issuer` in discovery document raises ValueError → HTTP 401
- [x] Malformed JWT (not 3 parts) raises OidcVerifyError → ValueError → HTTP 401
- [x] Non-decodable JWT header raises OidcVerifyError → ValueError → HTTP 401
- [x] Wrong signature raises OidcVerifyError → ValueError → HTTP 401
- [x] Wrong issuer claim raises OidcVerifyError → ValueError → HTTP 401
- [x] Wrong audience claim raises OidcVerifyError → ValueError → HTTP 401
- [x] Expired token raises OidcVerifyError → ValueError → HTTP 401
- [x] Unsupported JWT algorithm raises OidcVerifyError → ValueError → HTTP 401
- [x] Non-decodable ID token payload (bad padding) returns empty claims dict — `_decode_id_token_claims`
- [x] Malformed ID token (not 3 parts) returns empty claims dict

### JIT provisioning errors

- [x] RuntimeError when no organisation exists raises ValueError → HTTP 401

### Admin CRUD errors

- [x] Invalid JSON in `MODULO_OIDC_PROVIDERS` returns empty list with warning
- [x] Empty `client_id` in connection test skips `client_id_validated` field
- [x] Update endpoint with empty body returns 400 "No fields to update"
- [x] Provider not found on update/delete/toggle returns 404
- [x] SAML connection test returns 404 for unknown provider
- [x] All admin endpoints catch `ProgrammingError` → 501 Not Implemented
- [x] Duplicate provider name raises ValueError → 409 Conflict

## Resilience & Integration Robustness

### HTTP client configuration

- [x] Discovery document fetch has 10s read timeout, 5s connect timeout
- [x] Code exchange POST has 15s timeout
- [x] JWKS fetch has 10s timeout
- [x] SAML metadata fetch has 15s timeout

### Discovery document

- [x] HTTP errors during discovery fetch are caught and converted to ValueError
- [x] Missing `authorization_endpoint` is detected and reported
- [x] Missing `token_endpoint` is detected and reported
- [x] Missing `jwks_uri`/`issuer` is detected and reported
- [x] Schema drift (extra/missing fields) does not cause crashes — `.get()` used throughout
- [x] Non-JSON response from discovery URL raises ValueError

### JWKS cache

- [x] In-memory cache with 1-hour TTL
- [x] Cache invalidation on verification failure (key rotation)
- [x] Cache miss outside TTL rediscards stale entry
- [x] Cache cleared and retried on `kid` not found in JWKS
- [x] Cache cleared and retried on signature verification failure
- [x] `clear_jwks_cache()` available for manual invalidation

### Token exchange

- [x] HTTP errors during code exchange are caught and converted to ValueError
- [x] Missing `id_token` in token response raises ValueError (empty string triggers no-verify fallback)

### IdP unreachability

- [x] Login redirect: IdP unreachable raises ValueError → HTTP 400
- [x] Callback: discovery IdP unreachable raises ValueError → HTTP 401
- [x] Callback: token endpoint IdP unreachable raises ValueError → HTTP 401
- [x] Callback: JWKS endpoint IdP unreachable raises OidcVerifyError → ValueError → HTTP 401
- [x] Test connection: IdP unreachable returns failed SsoProviderTestResult (not crash)

## Additional Edge Cases

### State/CSRF

- [x] Empty state string returns None from verify_state → 401
- [x] Malformed state (no colon) returns None → 401
- [x] State signed with wrong key returns None → 401
- [x] Tampered state (extra chars) returns None → 401

### Provider parsing

- [x] Empty `MODULO_OIDC_PROVIDERS` returns empty list
- [x] Invalid JSON returns empty list with warning
- [x] Entries missing required fields are skipped with per-entry warning
- [x] Provider ID not found returns 400 in login, 401 in callback
- [x] Case-sensitive provider ID matching

### ID token claims

- [x] Missing `email` falls back to `sub` claim
- [x] Missing `name` falls back to `preferred_username` then email prefix
- [x] Empty `groups` claim (empty list) treated as no groups — skipped
- [x] Missing `groups` claim treated as no groups — skipped
- [x] Non-list `groups` claim (string) would cause AttributeError — not enforced

### Group mapping

- [x] Empty group mappings list on provider — skipped
- [x] Provider not found by client_id — skipped (not an error)
- [x] Unmatched IdP groups silently ignored
- [x] Multiple IdP groups map to multiple team memberships
- [x] Existing team membership with different role is updated

## Known Gaps

- [ ] No ID token signature verification when JWKS URI or issuer is absent from discovery document — falls back to unverified decode (documented in code)
- [ ] `MODULO_OIDC_PROVIDERS` env var approach deprecated in favour of DB-backed admin UI — remove in v1 cleanup
- [ ] SCIM provisioning deferred to v2
- [ ] No end-to-end integration test with a real OIDC provider (Google/GitHub/Okta)
- [ ] OIDC logout / session termination not implemented — tokens must expire naturally
- [ ] No refresh token rotation for OIDC-initiated sessions (token family is created but refresh flow not tested end-to-end)
- [ ] Group mapping test coverage is limited to unit tests with mocked provider lookups — no integration test with seeded DB provider
- [ ] `oidc_verify.py` depends on `python-jose` library — consider migrating to `PyJWT` with JWKS support for reduced dependency footprint
- [ ] Non-list `groups` claim (string) in ID token would cause AttributeError during `claims.get("groups", []) or []` — no defensive type guard

## QA History

### 2026-07-04 — Cross-cutting architecture QA (index 158)
- **Critical**: `httpx.HTTPError` from `_fetch_discovery` and `_exchange_code` inside `oidc_process_callback` propagated as 500 instead of 401 — both wrapped in try/except → ValueError
- **Critical**: `RuntimeError` from `jit_provision_user` propagated as 500 instead of 401 — wrapped in try/except → ValueError
- **Critical**: `httpx.HTTPError` from `_fetch_discovery` in `oidc_get_authorize_url` propagated as 500 instead of 400 — wrapped in try/except → ValueError
- **Major**: `_fetch_discovery` had no connect timeout — could hang on DNS/TCP failure — added `httpx.Timeout(10.0, connect=5.0)`
- **Major**: `test_jit_raises_if_no_org` patched `get_user_by_email` which was renamed to `get_account_by_email` — test was broken
- **Major**: Product map error paths section lines 139–140 incorrectly described `raise_for_status()` as raising `ValueError` (it raises `httpx.HTTPStatusError`) and claimed discovery HTTP errors propagate as 401 (they propagated as 500) — both fixed
- **Minor**: Missing tests for discovery HTTP error, code exchange HTTP error, and provisioning RuntimeError in callback — 3 new tests added to `test_oidc_verify.py`
