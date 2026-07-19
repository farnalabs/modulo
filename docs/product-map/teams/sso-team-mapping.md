---
id: feat-teams-sso-team-mapping
prd: 9.4, 6.2, 9.2
delivery-tasks: [task-nv6-sso-team-mapping]
bdd:
  - backend/tests/bdd/features/auth/sso_team_mapping.feature
code:
  - backend/src/modulo/auth/sso.py
  - backend/src/modulo/api/routes/admin_sso.py
  - backend/src/modulo/db/models/sso_provider.py
  - backend/src/modulo/db/crud/sso_provider.py
unit-tests:
  - backend/tests/unit/api/test_admin_sso.py
  - backend/tests/unit/auth/test_sso.py
  - backend/tests/unit/auth/test_sso_team_mapping_bdd.py
depends-on: [feat-teams-team-crud, feat-auth-sso-provider-ui]
status: partial
---
# SSO Team Mapping

Group-to-team mapping from OIDC/SAML identity provider group claims to Modulo team memberships, configured per SSO provider and applied at JIT user provisioning.

## Behaviours

### Admin group mapping configuration
- [x] Admin can read group mappings per SSO provider: `GET /api/v1/admin/sso/providers/{id}/group-mappings`
- [x] Admin can set group mappings per SSO provider: `PUT /api/v1/admin/sso/providers/{id}/group-mappings`
- [x] Group mapping format validated: `{idp_group: string, team_id: string, team_role: string}`
- [x] Default team_role for SSO group mapping is `viewer`
- [x] Non-admin users get 403 on all group mapping endpoints

### OIDC group-to-team mapping flow
- [x] Extracts `groups` claim from OIDC ID token during callback
- [x] Looks up DB provider by `client_id` to find group mapping configuration
- [x] Calls `apply_group_mappings` when groups present and mappings configured
- [x] Skip group mapping when no `groups` claim in ID token
- [x] Skip group mapping when no group mappings configured on provider

### SAML group-to-team mapping flow
- [x] Extracts groups attribute from SAML AttributeStatement (tries `groups`, `memberOf`, `Group`)
- [x] Comma-separated group string split into list
- [x] Looks up DB provider by `entity_id` to find group mapping configuration
- [x] Calls `apply_group_mappings` when groups attribute present and mappings configured
- [x] Skip group mapping when no groups attribute in SAML response
- [x] Skip group mapping when no group mappings configured on provider

### Group mapping application
- [x] Matching `idp_group` creates team membership with configured `team_role`
- [x] Non-matching groups silently skipped
- [x] Empty groups list silently skipped
- [x] Existing membership with different role is updated to the mapped role
- [x] Duplicate membership is not re-added
- [x] Group mapping applied on JIT provision (new user creation)
- [x] Group mapping applied on existing user re-authentication (re-linking)

### Enterprise feature gating
- [x] SSO is flagged as enterprise-tier feature (`feature_flags.py` name: `sso`)
- [x] Group mapping admin endpoints require admin `org_role`
- [x] Group mapping only available when enterprise license key is present

### Error Handling
- [x] Admin group mapping endpoints return 401 for unauthenticated requests
- [x] Admin group mapping endpoints return 403 for non-admin users
- [x] PUT group mappings returns 422 for invalid mapping format
- [x] OIDC callback returns 401 when ID token validation fails
- [x] SAML ACS returns 401 when SAML response validation fails (signature, expiry, destination)
- [x] IdP unreachable during callback raises ValueError → 400/401, no degraded mode
- [x] SSO provider lookup by `client_id` returns 404 if provider not found
- [x] ProgrammingError caught → 501 on all DB-accessing handlers (admin routes + OIDC/SAML callback routes)

### Resilience
- [x] External IdP HTTP failures (discovery, token exchange, metadata fetch) caught and wrapped as ValueError
- [x] XML parse failures (SAML response, IdP metadata) caught and wrapped as ValueError
- [x] DB unavailable → ProgrammingError caught at route level → 501 Not Implemented
- [x] SQLAlchemy errors → caught at route level → 503 Service Unavailable
- [x] Audit event logging failures are non-fatal (caught with log warning) — does not block SSO flow
- [x] Invalid group mapping entries (bad team_id UUID, missing keys) logged and skipped, not fatal
- [x] SAML clock skew detected and logged (5-minute tolerance) — does not block authentication

### Edge Cases
- [x] OIDC token with no `email` claim falls back to `sub` claim
- [x] OIDC token with neither `email` nor `sub` raises error
- [x] SAML response with no email attribute falls back to NameID
- [x] SAML response with no email or NameID raises error
- [x] OIDC provider with null/empty client_id — `_lookup_provider_by_client_id` may return None silently
- [x] SAML response with malformed base64/base64 padding → caught
- [x] SAML response with invalid XML → caught (defusedxml)
- [x] Duplicate SSO provider names per org → caught via `with_for_update()` + ValueError
- [x] OIDC `groups` claim is not a list → coerced to `[]` (groups mapping silently skipped)
- [x] SAML groups attribute with leading/trailing whitespace → stripped via `.strip()`

## Known Gaps

- SSO provider lookup during OIDC callback uses `client_id` — no fallback if provider has empty/null `client_id`
- `MODULO_OIDC_PROVIDERS` env var approach deprecated in favour of DB-backed admin UI — migration layer may lose group mapping config
- No integration tests exist for the full OIDC/SAML group mapping flow (only unit tests with mocked sessions)
- OIDC callback does not have a `try/except` for `_lookup_provider_by_client_id` or `apply_group_mappings` — a ProgrammingError from within these would propagate to the route handler's ProgrammingError catch, but a non-SQL error (e.g. ValueError from bad UUID in group_mappings) would surface as 401 instead of a more specific error
- `oidc_get_authorize_url` and `saml_get_auth_url` handle IdP unreachable as ValueError → 400, but the frontend receives no structured error — the user sees a generic "Bad Request" page
- SAML clock skew detection logs a warning but does not expose this to the frontend — admins troubleshooting failed logins see no clock skew indicator
- `apply_group_mappings` silently skips mappings with invalid `team_id` UUID (logs a warning only) — no feedback to the admin who configured the mapping
- No frontend view exists for configuring group mappings — only accessible via API/MCP
- OIDC `groups` claim type coercion (`not isinstance(raw_groups, list) → raw_groups = []`) silently discards non-list values — an IdP sending groups as a comma-separated string loses the data entirely 
