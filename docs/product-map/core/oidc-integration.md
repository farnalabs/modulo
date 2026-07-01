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

status: covered
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

### Error paths (discovered during audit)

- [x] Invalid JSON in `MODULO_OIDC_PROVIDERS` returns empty list with warning
- [x] Malformed ID token (not 3 parts) returns empty claims dict
- [x] Non-decodable ID token payload returns empty claims dict
- [x] `_exchange_code` HTTP errors propagate as `ValueError` via `raise_for_status()`
- [x] OIDC callback `httpx.HTTPError` during discovery fetch propagates as HTTP 401
- [x] Missing `token_endpoint` in discovery document raises ValueError
- [x] Missing `authorization_endpoint` in login raises ValueError → HTTP 400
- [x] Empty `client_id` in connection test skips `client_id_validated` field
- [x] Update endpoint with empty body returns 400 "No fields to update"
- [x] Provider not found on update/delete/toggle returns 404
- [x] SAML connection test returns 404 for unknown provider

## Known Gaps

- [ ] No ID token signature verification when JWKS URI or issuer is absent from discovery document — falls back to unverified decode (documented in code)
- [ ] `MODULO_OIDC_PROVIDERS` env var approach deprecated in favour of DB-backed admin UI — remove in v1 cleanup
- [ ] SCIM provisioning deferred to v2
- [ ] No end-to-end integration test with a real OIDC provider (Google/GitHub/Okta)
- [ ] OIDC logout / session termination not implemented — tokens must expire naturally
- [ ] No refresh token rotation for OIDC-initiated sessions (token family is created but refresh flow not tested end-to-end)
- [ ] Group mapping test coverage is limited to unit tests with mocked provider lookups — no integration test with seeded DB provider
- [ ] `oidc_verify.py` depends on `python-jose` library — consider migrating to `PyJWT` with JWKS support for reduced dependency footprint
