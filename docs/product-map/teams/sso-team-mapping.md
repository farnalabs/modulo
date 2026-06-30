---
id: feat-teams-sso-team-mapping
prd: 9.4, 6.2, 9.2
delivery-tasks: [task-nv6-sso-team-mapping]
code:
  - backend/src/modulo/auth/sso.py
  - backend/src/modulo/api/routes/admin_sso.py
  - backend/src/modulo/db/models/sso_provider.py
  - backend/src/modulo/db/crud/sso_provider.py
unit-tests:
  - backend/tests/unit/api/test_admin_sso.py
  - backend/tests/unit/auth/test_sso.py
depends-on: [feat-teams-team-crud, feat-auth-sso-provider-ui]
status: partial
---
# SSO Team Mapping Group-to-team mapping from OIDC/SAML identity provider group claims to Modulo team memberships, configured per SSO provider and applied at JIT user provisioning.

## Behaviours

### Admin group mapping configuration
- [ ] Admin can read group mappings per SSO provider: `GET /api/v1/admin/sso/providers/{id}/group-mappings`
- [ ] Admin can set group mappings per SSO provider: `PUT /api/v1/admin/sso/providers/{id}/group-mappings`
- [ ] Group mapping format validated: `{idp_group: string, team_id: string, team_role: string}`
- [ ] Default team_role for SSO group mapping is `viewer`
- [ ] Non-admin users get 403 on all group mapping endpoints

### OIDC group-to-team mapping flow
- [ ] Extracts `groups` claim from OIDC ID token during callback
- [ ] Looks up DB provider by `client_id` to find group mapping configuration
- [ ] Calls `apply_group_mappings` when groups present and mappings configured
- [ ] Skip group mapping when no `groups` claim in ID token
- [ ] Skip group mapping when no group mappings configured on provider

### SAML group-to-team mapping flow
- [ ] Extracts groups attribute from SAML AttributeStatement (tries `groups`, `memberOf`, `Group`)
- [ ] Comma-separated group string split into list
- [ ] Looks up DB provider by `entity_id` to find group mapping configuration
- [ ] Calls `apply_group_mappings` when groups attribute present and mappings configured
- [ ] Skip group mapping when no groups attribute in SAML response
- [ ] Skip group mapping when no group mappings configured on provider

### Group mapping application
- [ ] Matching `idp_group` creates team membership with configured `team_role`
- [ ] Non-matching groups silently skipped
- [ ] Empty groups list silently skipped
- [ ] Existing membership with different role is updated to the mapped role
- [ ] Duplicate membership is not re-added
- [ ] Group mapping applied on JIT provision (new user creation)
- [ ] Group mapping applied on existing user re-authentication (re-linking)

### Enterprise feature gating
- [ ] SSO is flagged as enterprise-tier feature (`feature_flags.py` name: `sso`)
- [ ] Group mapping admin endpoints require admin `org_role`
- [ ] Group mapping only available when enterprise license key is present

### Known Gaps
- [ ] No BDD `.feature` files exist for SSO group-to-team mapping
- [ ] SSO provider lookup during OIDC callback uses `client_id` — no fallback if provider has empty/null `client_id`
- [ ] `MODULO_OIDC_PROVIDERS` env var approach deprecated in favour of DB-backed admin UI — migration layer may lose group mapping config 