---
id: feat-core-saml-integration
prd: 9.4, 6.2, 9.2
delivery-tasks: [task-nv6-saml-integration]
bdd:
code:
  - backend/src/modulo/auth/sso.py
  - backend/src/modulo/api/routes/sso.py
  - backend/src/modulo/api/routes/admin_sso.py
  - backend/src/modulo/settings.py
unit-tests:
  - backend/tests/unit/auth/test_sso.py
  - backend/tests/unit/api/test_admin_sso.py

status: partial
---
# SAML 2.0 Integration SAML 2.0 SSO with HTTP-Redirect AuthnRequest, HTTP-POST ACS, IdP metadata parsing, JIT user provisioning, and group-to-team mapping. ## Behaviours ### AuthnRequest generation
- [ ] `GET /api/v1/auth/saml/login` redirects to IdP with SAMLRequest (HTTP 307)
- [ ] AuthnRequest XML contains correct Issuer from `modulo_saml_entity_id`
- [ ] AuthnRequest uses HTTP-Redirect binding with deflated + base64-encoded XML
- [ ] NameIDPolicy requests emailAddress format
- [ ] AssertionConsumerServiceURL points to configured `MODULO_PUBLIC_URL + /api/v1/auth/saml/acs` ### ACS endpoint
- [ ] `POST /api/v1/auth/saml/acs` accepts SAMLResponse from form data
- [ ] Validates SAML Conditions NotBefore (returns 401 if used before)
- [ ] Validates SAML Conditions NotOnOrAfter (returns 401 if expired)
- [ ] Warns on clock skew exceeding 5 minutes from IssueInstant
- [ ] Extracts NameID from SAML Assertion Subject
- [ ] Extracts email, displayName, groups from SAML AttributeStatement
- [ ] Falls back to NameID text as email when email attribute is missing
- [ ] Falls back to email prefix as display_name when displayName is missing
- [ ] Creates JIT-provisioned user with `auth_provider='saml'`
- [ ] Sets `sso_subject` to `saml:{idp_entity_id}:{name_id}`
- [ ] Issues JWT access + refresh tokens on success
- [ ] Redirects browser to frontend callback URL with tokens
- [ ] Calls `apply_group_mappings` when groups attribute present and group mappings configured ### SP metadata endpoint
- [ ] `GET /api/v1/auth/saml/metadata` returns SPSSODescriptor XML (text/plain)
- [ ] Metadata contains correct entityID from `modulo_saml_entity_id`
- [ ] Metadata contains correct ACS Location URL ### Enterprise license gating
- [ ] SAML login returns 402 when license key is absent
- [ ] SAML ACS returns 402 when license key is absent
- [ ] SAML metadata endpoint returns 402 when license key is absent
- [ ] SAML login returns 400 when SAML is not enabled in settings
- [ ] SAML ACS returns 400 when SAML is not enabled in settings
- [ ] `/api/v1/auth/sso/providers` exposes `saml: false` when license is absent
- [ ] `/api/v1/auth/sso/providers` exposes `saml: true` only when license present + configured ### IdP metadata handling
- [ ] Supports inline metadata XML via `modulo_saml_idp_metadata_xml`
- [ ] Supports remote metadata URL via `modulo_saml_idp_metadata_url`
- [ ] Inline XML takes precedence over URL
- [ ] Returns error when neither metadata source is configured
- [ ] Parses IdP entityID from metadata EntityDescriptor
- [ ] Prefers HTTP-Redirect SingleSignOnService binding
- [ ] Falls back to first SingleSignOnService when HTTP-Redirect binding not found
- [ ] Raises error when IDPSSODescriptor is missing from metadata ### SSO provider admin CRUD
- [ ] Admin can create SAML provider with metadata_url or metadata_xml
- [ ] Admin can update SAML provider fields
- [ ] Admin can delete SAML provider
- [ ] Admin can toggle provider enabled/disabled
- [ ] Provider type is validated (must be `oidc` or `saml`)
- [ ] Default role is validated (must be `operator` or `runner`)
- [ ] Non-admin users get 403 on all admin SSO endpoints ### Connection testing
- [ ] Admin can test SAML provider connection
- [ ] Test fetches metadata from URL if configured
- [ ] Test parses metadata XML and validates IDPSSODescriptor
- [ ] Test reports entity ID, SSO URL, and certificates
- [ ] Test returns failure details on invalid XML or missing descriptor ### Group-to-team mapping
- [ ] Admin can set group mappings per provider: `{idp_group, team_id, team_role}`
- [ ] Admin can read group mappings per provider
- [ ] On JIT provision, matching groups apply team membership
- [ ] Non-matching groups are silently skipped
- [ ] Empty mappings are silently skipped
- [ ] Existing membership with different role is updated
- [ ] Duplicate membership is not re-added ### JIT provisioning edge cases
- [ ] Existing user matched by email gets `sso_subject` and `auth_provider` updated
- [ ] Missing organisation raises RuntimeError
- [ ] Custom `default_org_id` bypasses org lookup
- [ ] Default org role from `modulo_sso_default_role` applied ### Known gaps
- [ ] No SAML Response signature verification — SP private key / cert not wired
- [ ] ID token signature verification not performed (documented, shared with OIDC)
- [ ] No SAML Single Logout (SLO) endpoint
- [ ] No BDD `.feature` files for SAML scenarios exist
- [ ] SCIM provisioning deferred to v2
- [ ] `modulo_saml_sp_private_key` and `modulo_saml_sp_x509_cert` settings defined but unused 