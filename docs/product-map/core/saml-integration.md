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
  - backend/tests/unit/auth/test_saml_parse_datetime.py
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

### Error Handling

- [x] `saml_process_response` catches `binascii.Error` (base64 decode fail) → re-raises as ValueError → ACS route returns 401
- [x] `saml_process_response` catches `ET.ParseError` (XML parse fail) → re-raises as ValueError → ACS route returns 401
- [x] `saml_process_response` catches `UnicodeDecodeError` (non-UTF-8 SAML response) → re-raises as ValueError → ACS route returns 401
- [x] `saml_process_response` catches `httpx.HTTPError` from IdP metadata fetch → re-raises as ValueError → route handler returns 400/401
- [x] `saml_process_response` catches `ET.ParseError` from IdP metadata parse → re-raises as ValueError → route handler returns 400/401
- [x] `saml_process_response` catches `RuntimeError` from `jit_provision_user` → re-raises as ValueError → route handler returns 401
- [x] `saml_get_auth_url` catches `httpx.HTTPError` from IdP metadata fetch → re-raises as ValueError → login route returns 400
- [x] `saml_get_auth_url` catches `ET.ParseError` from IdP metadata parse → re-raises as ValueError → login route returns 400
- [x] `saml_get_auth_url` raises `ValueError` when SSO URL empty in metadata → login route returns 400
- [x] `saml_login` route catches `ProgrammingError` from DB operations → returns 501 Not Implemented
- [x] `saml_acs` route catches `ProgrammingError` from DB operations → returns 501 Not Implemented
- [x] `_fetch_discovery` catches `json.JSONDecodeError` from non-JSON discovery response → re-raises as ValueError
- [x] Every `raise ValueError` in `saml_process_response` is logged with entity_id context via `_log.warning`
- [x] `saml_process_response` handles missing SAMLResponse form field — route returns 400 with specific message
- [x] `_test_saml_connection` returns failure details on invalid XML, missing descriptor, fetch failure
- [x] `saml_acs` catches `RuntimeError` from JIT provisioning → returns 401
- [x] Non-admin users get 403 on all admin SSO endpoints
- [x] SAML login/ACS/metadata routes all gated by `require_feature("sso")` → Community tier returns 402

### Edge Cases & Boundaries

- [x] Empty SAMLResponse form field returns 400
- [x] Garbled base64 SAMLResponse returns 401 (binascii.Error caught)
- [x] Malformed XML SAMLResponse returns 401 (ET.ParseError caught)
- [x] Non-UTF-8 SAMLResponse returns 401 (UnicodeDecodeError caught)
- [ ] Missing SAMLResponse Destination attribute validation (SAML 2.0 Core §4.1.1) — not implemented
- [ ] Missing InResponseTo validation on SAML Response (replay risk) — not implemented
- [x] Missing Subject/NameID produces empty email → caught by jit_provision_user RuntimeError or DB constraint
- [x] Missing email attribute falls back to NameID text, then empty string
- [x] Missing displayName falls back to email prefix, then raw email
- [x] Groups attribute in `memberOf` or `Group` names (not just `groups`) — code handles, tests cover `groups` only
- [x] Metadata URL fetch failure (network error, HTTP error) → returns 400
- [x] Metadata URL returns non-XML content → ET.ParseError caught → returns 400
- [x] IdP metadata missing `entityID` attribute → produces `saml::name_id` sso_subject (empty entity_id segment)
- [x] IdP metadata missing IDPSSODescriptor → ValueError raised
- [ ] Team deleted after group mapping configured → FK cascade removes memberships, re-add on next login may hit FK violation (not tested)
- [x] Empty sso_url from metadata (no Location) → ValueError raised
- [x] SAML Conditions NotBefore validation (returns 401 if before time)
- [x] SAML Conditions NotOnOrAfter validation (returns 401 if expired)
- [x] Clock skew warning logged for IssueInstant > 5 min difference
- [x] Unparseable IssueInstant logs warning and continues (no rejection)
- [x] Unparseable NotBefore/NotOnOrAfter raises ValueError

### Resilience & Integration Robustness

- [x] IdP metadata URL fetch has httpx.Timeout with separate connect=5s timeout
- [x] Inline metadata XML takes precedence over URL (no network dependency when inline configured)
- [x] Metadata URL fetch failure degrades gracefully (returns 400, not 500)
- [x] IdP metadata parse errors degrade gracefully (returns 400, not 500)
- [x] DB `ProgrammingError` returns 501 Not Implemented (pre-migration state)
- [x] `_test_saml_connection` prefers HTTP-Redirect binding (consistent with login code)
- [x] Connection test reports entity ID, SSO URL, and certificate info
- [x] Connection test reports failure details on invalid XML/missing descriptor
- [ ] Zero retry or rate-limit handling across all external HTTP calls (no exponential backoff)
- [ ] No connection pooling — new `httpx.AsyncClient()` per call
- [ ] No SAML Response signature verification — relies on transport-level HTTPS only
- [ ] No namespace fallback for non-SAML2.0 IdP metadata (hardcoded `urn:oasis:names:tc:SAML:2.0:metadata`)

### QA History

- 2026-07-05: Cross-cutting QA (index 160). Fixed CRITICAL — unguarded base64/ET.parse/UnicodeDecode exceptions in `saml_process_response` now re-raised as ValueError → 401. Fixed CRITICAL — uncaught RuntimeError from `jit_provision_user` in SAML path now caught (matching OIDC path). Fixed CRITICAL — uncaught httpx.HTTPError/ET.ParseError from metadata fetch in both SAML endpoints now caught → 400/401. Fixed CRITICAL — missing ProgrammingError→501 catch in `saml_acs` and `saml_login` routes (only admin SSO routes had it). Fixed CRITICAL — empty sso_url from metadata now raises ValueError instead of producing broken redirect. Fixed MAJOR — httpx.Timeout with connect=5s on SAML metadata HTTP calls. Fixed MAJOR — structured logging in saml_process_response failure paths with idp_entity_id context. Fixed MAJOR — _test_saml_connection now prefers HTTP-Redirect binding (consistent with login code). Fixed MAJOR — added 6 direct unit tests for _parse_saml_datetime. Fixed MAJOR — cleaned up redundant exception type list. Added Error Handling section (17 checkboxes). Added Edge Cases section (17 checkboxes: 13 [x] + 4 [ ] including Destination/InResponseTo validation, team deletion FK gap, `memberOf`/`Group` test gap). Added Resilience section (12 checkboxes: 8 [x] + 4 [ ] including retry, pooling, signature verification, namespace fallback). Created website docs stub. All 95 SAML/SSO unit tests pass. Status: partial (14 known gaps + 4 unchecked edge cases + 4 unchecked resilience items).

### Known gaps

- [ ] No SAML Response signature verification — SP private key / cert not wired
- [ ] ID token signature verification not performed (documented, shared with OIDC)
- [ ] No SAML Single Logout (SLO) endpoint
- [ ] SCIM provisioning deferred to v2
- [ ] `modulo_saml_sp_private_key` and `modulo_saml_sp_x509_cert` settings defined but unused
- [ ] No integration test for SAML ACS with real XML parsing (all existing tests mock `_saml_fetch_idp_metadata`)
- [ ] Concurrent SAML ACS requests for same unregistered email may cause unique constraint crash (no DB-level locking around JIT provision check-and-create)
- [ ] Missing SAML Response `Destination` validation (SAML 2.0 Core §4.1.1 — not implemented)
- [ ] Missing `InResponseTo` validation on SAML Response (replay risk — not implemented)
- [ ] Team deletion breaks group mappings — FK cascade removes membership, re-add on next login may hit FK violation
- [ ] `memberOf`/`Group` attribute name fallbacks in group lookup not tested (code handles them, tests only cover `groups`)
- [ ] Zero retry/rate-limit handling on external HTTP calls — one-shot with no backoff
- [ ] No connection pooling — new `httpx.AsyncClient()` per HTTP call
- [ ] No namespace fallback for non-SAML2.0 IdP metadata (hardcoded to `urn:oasis:names:tc:SAML:2.0:metadata`) 