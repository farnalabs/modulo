---
id: feat-core-saml-integration
prd: 9.4, 6.2, 9.2
delivery-tasks: [task-nv6-saml-integration]
bdd:
  - backend/tests/bdd/features/auth/sso_saml.feature
code:
  - backend/src/modulo/auth/sso.py
  - backend/src/modulo/api/routes/sso.py
  - backend/src/modulo/api/routes/admin_sso.py
  - backend/src/modulo/settings.py
unit-tests:
  - backend/tests/unit/auth/test_sso.py
  - backend/tests/unit/api/test_admin_sso.py
  - backend/tests/unit/auth/test_sso_saml_bdd.py
depends-on: [feat-auth-jwt-auth, feat-teams-team-isolation]
status: partial
---

# SAML 2.0 Integration

SAML 2.0 SSO with HTTP-Redirect AuthnRequest, HTTP-POST ACS, IdP metadata parsing, JIT user provisioning, and group-to-team mapping.

## Behaviours

### AuthnRequest generation

- [x] `GET /api/v1/auth/saml/login` redirects to IdP with SAMLRequest (HTTP 307)
- [x] AuthnRequest XML contains correct Issuer from `modulo_saml_entity_id`
- [x] AuthnRequest uses HTTP-Redirect binding with deflated + base64-encoded XML
- [x] NameIDPolicy requests emailAddress format
- [x] AssertionConsumerServiceURL points to configured `MODULO_PUBLIC_URL + /api/v1/auth/saml/acs`

### ACS endpoint

- [x] `POST /api/v1/auth/saml/acs` accepts SAMLResponse from form data
- [x] Validates SAML Conditions NotBefore (returns 401 if used before)
- [x] Validates SAML Conditions NotOnOrAfter (returns 401 if expired)
- [x] Warns on clock skew exceeding 5 minutes from IssueInstant
- [x] Extracts NameID from SAML Assertion Subject
- [x] Extracts email, displayName, groups from SAML AttributeStatement
- [x] Falls back to NameID text as email when email attribute is missing
- [x] Falls back to email prefix as display_name when displayName is missing
- [x] Creates JIT-provisioned user with `auth_provider='saml'`
- [x] Sets `sso_subject` to `saml:{idp_entity_id}:{name_id}`
- [x] Issues JWT access + refresh tokens on success
- [x] Redirects browser to frontend callback URL with tokens
- [x] Calls `apply_group_mappings` when groups attribute present and group mappings configured

### SP metadata endpoint

- [x] `GET /api/v1/auth/saml/metadata` returns SPSSODescriptor XML (text/plain)
- [x] Metadata contains correct entityID from `modulo_saml_entity_id`
- [x] Metadata contains correct ACS Location URL

### Enterprise license gating

- [x] SAML login returns 402 when license key is absent
- [x] SAML ACS returns 402 when license key is absent
- [x] SAML metadata endpoint returns 402 when license key is absent
- [x] SAML login returns 400 when SAML is not enabled in settings
- [x] SAML ACS returns 400 when SAML is not enabled in settings
- [x] `/api/v1/auth/sso/providers` exposes `saml: false` when license is absent
- [x] `/api/v1/auth/sso/providers` exposes `saml: true` only when license present + configured

### IdP metadata handling

- [x] Supports inline metadata XML via `modulo_saml_idp_metadata_xml`
- [x] Supports remote metadata URL via `modulo_saml_idp_metadata_url`
- [x] Inline XML takes precedence over URL
- [x] Returns error when neither metadata source is configured
- [x] Parses IdP entityID from metadata EntityDescriptor
- [x] Prefers HTTP-Redirect SingleSignOnService binding
- [x] Falls back to first SingleSignOnService when HTTP-Redirect binding not found
- [x] Raises error when IDPSSODescriptor is missing from metadata

### SSO provider admin CRUD

- [x] Admin can create SAML provider with metadata_url or metadata_xml
- [x] Admin can update SAML provider fields
- [x] Admin can delete SAML provider
- [x] Admin can toggle provider enabled/disabled
- [x] Provider type is validated (must be `oidc` or `saml`)
- [x] Default role is validated (must be `operator` or `runner`)
- [x] Non-admin users get 403 on all admin SSO endpoints

### Connection testing

- [x] Admin can test SAML provider connection
- [x] Test fetches metadata from URL if configured
- [x] Test parses metadata XML and validates IDPSSODescriptor
- [x] Test reports entity ID, SSO URL, and certificates
- [x] Test returns failure details on invalid XML or missing descriptor

### Group-to-team mapping

- [x] Admin can set group mappings per provider: `{idp_group, team_id, team_role}`
- [x] Admin can read group mappings per provider
- [x] On JIT provision, matching groups apply team membership
- [x] Non-matching groups are silently skipped
- [x] Empty mappings are silently skipped
- [x] Existing membership with different role is updated
- [x] Duplicate membership is not re-added

### JIT provisioning edge cases

- [x] Existing user matched by email gets `sso_subject` and `auth_provider` updated
- [x] Missing organisation raises RuntimeError
- [x] Custom `default_org_id` bypasses org lookup
- [x] Default org role from `modulo_sso_default_role` applied

### Known gaps

- [ ] No SAML Response signature verification — SP private key / cert not wired
- [ ] ID token signature verification not performed (documented, shared with OIDC)
- [ ] No SAML Single Logout (SLO) endpoint
- [ ] SCIM provisioning deferred to v2
- [ ] `modulo_saml_sp_private_key` and `modulo_saml_sp_x509_cert` settings defined but unused
- [ ] No integration test for SAML ACS with real XML parsing (all existing tests mock `_saml_fetch_idp_metadata`)
- [ ] Login route (`/saml/login`) only gated by `require_feature("sso")` — no explicit check for missing SAMLResponse on login (different from ACS which validates form data) 