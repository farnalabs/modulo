---
id: feat-core-oidc-integration
prd: 9.4, 6.2, 9.2
delivery-tasks: [task-nv6-oidc-integration]
code:
  - backend/src/modulo/auth/sso.py
  - backend/src/modulo/api/routes/sso.py
  - backend/src/modulo/api/routes/admin_sso.py
  - backend/src/modulo/settings.py
  - backend/src/modulo/api/main.py
unit-tests:
  - backend/tests/unit/auth/test_sso.py

status: partial
---
# OIDC Integration

OpenID Connect SSO with authorization code flow, discovery document parsing, JIT user provisioning, and group-to-team mapping.

## Behaviours

### Provider configuration (env-var seeding)
- [ ] One-time migration from `MODULO_OIDC_PROVIDERS` env var to `sso_providers` DB table on startup
- [ ] Migration skips when env var is empty or `[]`
- [ ] Migration skips when providers already exist in DB
- [ ] Each env var entry must have `provider_id`, `client_id`, `client_secret`, `discovery_url` — entries missing fields are skipped with warning
- [ ] Seeded providers get scopes `["openid", "profile", "email"]` and default role from `modulo_sso_default_role` ### SSO providers endpoint
- [ ] `GET /api/v1/auth/sso/providers` returns list of configured OIDC providers with `provider_id`
- [ ] Returns `saml: bool` alongside OIDC list
- [ ] SAML is reported as enabled only when license present + SAML configured ### Authorization redirect
- [ ] `GET /api/v1/auth/oidc/{provider}/login` returns HTTP 307 redirect to IdP authorization endpoint
- [ ] Redirect URL includes `client_id`, `response_type=code`, `scope=openid email profile`, `redirect_uri`, signed `state`
- [ ] State is HMAC-SHA256 signed for CSRF protection
- [ ] Redirect URI derived from `MODULO_PUBLIC_URL`
- [ ] Returns 400 with error detail when provider ID is unknown
- [ ] Returns error when discovery document lacks `authorization_endpoint`
- [ ] Fetches discovery document from provider's `discovery_url` via HTTPS ### Callback / code exchange
- [ ] `GET /api/v1/auth/oidc/{provider}/callback` accepts `code` and `state` query params
- [ ] Returns 400 when `code` or `state` is missing
- [ ] Returns 401 when state signature verification fails (tampered/CSRF)
- [ ] Returns 401 when provider ID in state does not match any configured provider
- [ ] Exchanges authorization code at IdP token endpoint for `id_token`
- [ ] Token endpoint URL fetched from discovery document
- [ ] Token exchange uses `grant_type=authorization_code` with client credentials ### ID token processing
- [ ] Decodes ID token JWT payload (base64, no signature verification)
- [ ] Extracts `email`, `name`/`preferred_username`, `sub` from claims
- [ ] Falls back to `sub` claim as email when email is missing
- [ ] Falls back to `preferred_username` then email prefix as display name ### JIT user provisioning
- [ ] Existing user matched by email gets `sso_subject` and `auth_provider` updated to `oidc`
- [ ] New user created with `auth_provider='oidc'`, `password_hash=None`
- [ ] New user placed in first org (by `created_at`) or custom `default_org_id`
- [ ] `sso_subject` format: `{provider_id}:{sub}`
- [ ] Default org role from `modulo_sso_default_role` applied
- [ ] Raises RuntimeError when no organisation exists
- [ ] Custom `default_org_id` bypasses org lookup ### Group-to-team mapping
- [ ] Extracts `groups` claim from ID token
- [ ] Looks up DB provider by `client_id` for group mapping configuration
- [ ] Applies group mappings: matching groups create or update team membership
- [ ] Non-matching groups silently skipped
- [ ] Existing membership with different role is updated
- [ ] Duplicate membership is not re-added
- [ ] Skip group mapping when no groups in ID token
- [ ] Skip group mapping when no group mappings configured on provider ### Token issuance
- [ ] Issues JWT access token (15-min TTL) on successful OIDC login
- [ ] Issues JWT refresh token (7-day TTL) with token family rotation
- [ ] Tokens carry `org_role` claim
- [ ] Redirects browser to frontend callback URL with `access_token` and `refresh_token`
- [ ] Frontend base URL derived from first `CORS_ORIGINS` origin ### SSO provider admin CRUD
- [ ] Admin can create OIDC provider with `discovery_url`, `client_id`, `client_secret`
- [ ] Admin can update OIDC provider fields
- [ ] Admin can delete OIDC provider
- [ ] Admin can toggle provider enabled/disabled
- [ ] Provider type is validated (must be `oidc` or `saml`)
- [ ] Default role is validated (must be `operator` or `runner`)
- [ ] Provider scopes stored as JSON text column
- [ ] Non-admin users get 403 on all admin SSO endpoints
- [ ] `GET /api/v1/admin/sso/providers` lists all SSO providers ### Connection testing
- [ ] Admin can test OIDC provider connection
- [ ] Test fetches discovery document from provider URL
- [ ] Test validates presence of `authorization_endpoint` in discovery document
- [ ] Test reports issuer, auth/token/userinfo/JWKS endpoints, supported scopes
- [ ] Test returns failure details on network error or invalid document ### Group mapping admin
- [ ] Admin can set group mappings per provider: `PUT /api/v1/admin/sso/providers/{id}/group-mappings`
- [ ] Admin can read group mappings per provider: `GET /api/v1/admin/sso/providers/{id}/group-mappings`
- [ ] Group mapping format: `{idp_group, team_id, team_role}` ### Enterprise feature gating
- [ ] SSO is flagged as enterprise-tier feature
- [ ] OIDC login/callback does not require license key (unlike SAML)
- [ ] Admin SSO endpoints all require admin `org_role` ## Known Gaps
- [ ] No ID token signature verification — JWT payload decoded without JWKS validation (documented in code)
- [ ] No BDD `.feature` files for OIDC scenarios exist
- [ ] `oidc_verify.py` source file removed from repo but `.pyc` cache remains — orphaned
- [ ] `docs/product-map/auth/sso.md` referenced from `_index.md` as `SSO / OIDC` but file does not exist
- [ ] SCIM provisioning deferred to v2
- [ ] `MODULO_OIDC_PROVIDERS` env var approach deprecated in favour of DB-backed admin UI — remove in v1 cleanup 