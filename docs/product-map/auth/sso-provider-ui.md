---
id: feat-auth-sso-provider-ui
prd: 9.4
delivery-tasks: [task-nv6-sso-provider-ui]
code:
  - frontend/src/views/SettingsSsoView.vue
  - frontend/src/components/SsoProviderForm.vue
  - frontend/src/router/index.ts
  - frontend/src/config/navigation.ts
  - backend/src/modulo/api/routes/admin_sso.py
  - backend/src/modulo/api/routes/sso.py
  - backend/src/modulo/auth/sso.py
  - backend/src/modulo/db/crud/sso_provider.py
  - backend/src/modulo/db/models/sso_provider.py
bdd:
  - backend/tests/bdd/features/auth/sso_oidc.feature
  - backend/tests/bdd/features/auth/sso_saml.feature
  - backend/tests/bdd/features/auth/sso_team_mapping.feature
unit-tests:
  - backend/tests/unit/api/test_admin_sso.py
  - backend/tests/unit/api/test_sso_gating.py
  - backend/tests/unit/api/test_error_handling.py
  - backend/tests/unit/auth/test_sso.py
  - backend/tests/bdd/steps/test_sso_oidc.py
  - backend/tests/bdd/steps/test_sso_saml.py
  - backend/tests/bdd/steps/test_sso_team_mapping.py
depends-on: [feat-core-oidc-integration, feat-core-saml-integration, feat-auth-team-rbac]
status: partial
---
# SSO Provider UI

Admin settings page for configuring OIDC and SAML 2.0 identity providers. Team-gated feature (§9.4, license key feature `sso`).

## Behaviours

### Provider list and management
- [x] Admin can view list of all configured SSO providers with type badges (O / S)
- [x] Admin can add an OpenID Connect (OIDC) provider (client ID, client secret, discovery URL, scopes)
- [x] Admin can add a SAML 2.0 provider (metadata URL, metadata XML, entity ID)
- [x] Provider form shows conditional fields based on selected type (OIDC vs SAML)
- [x] Common fields per provider: name, auto-provision toggle, default role (runner/operator)
- [x] Admin can edit an existing SSO provider (inline form)
- [x] Admin can enable/disable an SSO provider with toggle
- [x] Admin can delete an SSO provider with confirmation dialog
- [x] Admin can test an SSO provider connection — OIDC resolves discovery URL, SAML parses metadata
- [x] Adds/edits/deletes raise audit events (`sso_provider.created`, `.updated`, `.deleted`, `.toggled`)
- [x] SSO provider management is admin-only (403 for non-admin users)

### Team gating
- [x] SSO settings page is hidden or locked behind team license check (`<FeatureGate show-disabled>`)
- [x] All 8 admin SSO routes return 402 when license lacks `sso` feature flag
- [x] Sidebar SSO nav entry is tier-gated (`<<: *team` → `required_tier: team` + `required_roles: [admin]` in manifest.yaml) but not feature-flag-gated — team-tier users see the link even if SSO is not enabled by license key; the `<FeatureGate show-disabled>` on the page itself shows a locked prompt. Verified in manifest.yaml (2026-08-15). The feature-flag-gating gap is documented in Known Gaps.

### API CRUD — Error handling
- [x] GET /providers returns 501 with "migrations" message on ProgrammingError (table missing)
- [x] POST /providers returns 501 with "migrations" message on ProgrammingError
- [x] PUT /providers/{id} returns 501 with "migrations" message on ProgrammingError
- [x] DELETE /providers/{id} returns 501 with "migrations" message on ProgrammingError
- [x] POST /providers/{id}/toggle returns 501 with "migrations" message on ProgrammingError
- [x] POST /providers/{id}/test returns 501 with "migrations" message on ProgrammingError (get_provider query)
- [x] PUT /providers/{id}/group-mappings returns 501 with "migrations" message on ProgrammingError
- [x] GET /providers/{id}/group-mappings returns 501 with "migrations" message on ProgrammingError
- [x] All ProgrammingError catches log a warning with the exception context
- [x] All routes return 401 for unauthenticated requests
- [x] All routes return 403 for non-admin users (operator/runner)
- [x] POST /providers returns 409 on duplicate provider name (with FOR UPDATE lock)
- [x] PUT /providers/{id} returns 400 on empty update body ("No fields to update")
- [x] PUT/DELETE/POST/test/PUT toggle return 404 when provider not found
- [x] POST /providers returns 422 on invalid provider_type (not oidc/saml)
- [x] POST /providers returns 422 on invalid default_role (not operator/runner)

### Test connection — OIDC
- [x] Missing discovery_url returns `success: false` with "Discovery URL is required"
- [x] Unreachable discovery URL returns `success: false` with fetch error message
- [x] Discovery URL returning non-JSON returns `success: false` with parse error
- [x] Missing `authorization_endpoint` in discovery doc returns `success: false`
- [x] Successful discovery returns `success: true` with provider info (issuer, endpoints, scopes)
- [x] All errors surfaced inline in the frontend (no page navigation)
- [x] Test result auto-dismisses after 12 seconds

### Test connection — SAML
- [x] Missing both metadata_url and metadata_xml returns `success: false`
- [x] Unreachable metadata URL returns `success: false` with fetch error message
- [x] Malformed metadata XML returns `success: false` with parse error
- [x] Missing IDPSSODescriptor in metadata XML returns `success: false`
- [x] Successful parse returns `success: true` with entity_id, SSO URL, certificate info
- [x] All errors surfaced inline in the frontend (no page navigation)
- [x] Test result auto-dismisses after 12 seconds

### Edge cases and error states
- [x] Add provider with empty required fields shows inline validation errors (silent `return` when name is empty)
- [x] Duplicate provider name is rejected (409 with FOR UPDATE lock)
- [x] Test connection on OIDC provider with invalid/unreachable discovery URL shows error inline
- [x] Test connection on SAML provider with invalid/empty metadata shows error inline
- [ ] Delete provider warns about effect on active user sessions — the confirmation dialog says "This action cannot be undone" only, with no SSO session-count/session-invalidation warning (not PRD-required; see Known Gaps)
- [x] SAML endpoints return 402 when team license is absent
- [x] Form state is preserved on validation failure (no page loss)
- [x] Test connection results show success or failure details inline (no page navigation)
- [x] Empty provider list shows "No SSO providers configured" empty state
- [x] Loading state shows LoadingSpinner component
- [x] Error state shows ErrorAlert with retry button (retryable for 5xx, non-retryable for 4xx)

### Authentication flow integration
- [ ] Configured OIDC providers appear on the login page as buttons — LoginView.vue has no SSO provider buttons (login is still password/Basic Auth); the `/api/v1/auth/oidc/{provider}/login` + callback endpoints exist and are reachable directly (see Known Gaps)
- [x] SAML SSO login is available when SAML is enabled + licensed — `GET /api/v1/auth/saml/login` + `POST /api/v1/auth/saml/acs` + `/metadata` exist and are license-gated (402 without license, 400 without metadata source); verified by test_sso.py `TestSamlRoutesExtended.test_saml_login_with_license_and_metadata` + BDD steps/test_sso_saml.py
- [x] JIT provisioning creates user on first SSO login with configured default role — `jit_provision_user` (auth/sso.py) provisions with the provider's default role; verified by test_sso.py `TestJitProvisioningExtended` (creates user, finds existing user and updates SSO provider, default-org handling) + BDD step "a new user account was provisioned" (test_sso_oidc.py)
- [x] Group-to-team mapping from `SsoProvider.group_mappings` applies at JIT provisioning — `apply_group_mappings` is invoked by both OIDC callback and SAML ACS when the DB provider has group_mappings; verified by sso_team_mapping.feature + test_sso_team_mapping.py step definitions ("apply_group_mappings was called with groups ...", role assignment)
- [x] OIDC callback exchanges auth code, verifies state, issues JWT — `oidc_process_callback` verifies the HMAC-signed state (CSRF), exchanges the code, verifies the id_token, provisions, and issues an access+refresh JWT pair; verified by test_sso.py `TestOidcProcessCallback.test_full_success_flow` + `TestOidcCallbackEndpointExtended.test_success_redirects_with_tokens` + BDD (test_sso_oidc.py: valid callback, tampered state → error)
- [x] SAML ACS parses `SAMLResponse`, validates assertion, issues JWT — `saml_process_response` parses the SAMLResponse XML, validates the assertion, provisions, and issues JWTs; verified by test_sso.py `TestSamlProcessResponse.test_full_success_flow` + `TestSamlRoutesExtended.test_saml_acs_with_license_and_valid_response` + BDD (test_sso_saml.py)

### Error Handling
- [x] GET /providers returns 503 on SQLAlchemyError
- [x] POST /providers returns 503 on SQLAlchemyError
- [x] PUT /providers/{id} returns 503 on SQLAlchemyError
- [x] DELETE /providers/{id} returns 503 on SQLAlchemyError
- [x] POST /providers/{id}/test returns 503 on SQLAlchemyError
- [x] PUT /providers/{id}/toggle returns 503 on SQLAlchemyError
- [x] PUT /providers/{id}/group-mappings returns 503 on SQLAlchemyError
- [x] GET /providers/{id}/group-mappings returns 503 on SQLAlchemyError
- [x] OIDC callback returns 503 on SQLAlchemyError
- [x] SAML login returns 503 on SQLAlchemyError
- [x] SAML ACS returns 503 on SQLAlchemyError
- [x] POST /providers returns 409 on IntegrityError (duplicate name race)
- [x] DELETE /providers/{id} returns 404 when provider not found
- [x] All SQLAlchemyError catches log a warning with operation context

## QA History
- **2026-08-15 — distribute (final-pass sweep C)**: Documented the unchecked delete-provider warning gap in Known Gaps — the confirmation dialog says only `This action cannot be undone` with no SSO session-count/invalidation warning (not PRD-required). The login-page-provider-buttons gap was already documented. Status: partial.
- 2026-08-15: Cross-cutting QA (distribute partial-auth2). Verified the "Authentication flow integration" section was STALE — 5 of 6 "v1 deferred" items are now implemented and tested (SAML login available when enabled+licensed, JIT provisioning with default role, group-to-team mapping at JIT, OIDC callback with state verification + JWT issuance, SAML ACS with assertion validation + JWT issuance). Marked them [x] with test references (test_sso.py unit tests + sso_oidc/sso_saml/sso_team_mapping BDD step files). The one remaining unchecked item — configured OIDC providers on the login page as buttons — is a genuine gap (LoginView.vue has no SSO buttons); moved to Known Gaps. Marked sidebar nav gating [~]→[x] (verified tier-gating in manifest.yaml; feature-flag gating remains a documented Known Gap). Kept the delete-provider session warning unchecked as not PRD-required. Status: partial (2 genuine gaps).
- 2026-07-07: Cross-cutting QA (index 319). Fixed CRITICAL — all user-facing strings in SettingsSsoView.vue (20 strings) and SsoProviderForm.vue (16 strings) converted to use `$t()` with i18n keys added to en-US.js. Fixed MAJOR — empty name validation (known gap #5): createProvider()/updateProvider() now shows inline error "Provider name is required" instead of silent `return`. Fixed MAJOR — toggle button now has loading state (`togglingId` ref, opacity+pointer-events during API call). Fixed MAJOR — auto-provision toggle accessibility: added `role="switch"`, `aria-checked`, keyboard handler (Enter/Space), tabindex. Fixed MAJOR — enable/disable toggle accessibility: added `aria-checked`. Removed hardcoded `aria-label` and `title` attributes. Updated product map: empty name validation [ ]→[x]. Status: partial.
- 2026-07-05: Cross-cutting QA (index 175). Fixed CRITICAL — added SQLAlchemyError→503 catches to all 8 admin SSO routes (get_providers, create, update, delete, toggle, test, set_group_mappings, get_group_mappings) and 3 SSO auth routes (oidc_callback, saml_login, saml_acs) — previously only caught ProgrammingError→501, allowing connection/deadlock failures to propagate as 500. Fixed CRITICAL — TOCTOU race in duplicate name check: `select(exists()...).with_for_update()` didn't lock rows; changed to proper `SELECT ... FOR UPDATE` and added IntegrityError→409 catch. Fixed MAJOR — DELETE endpoint returned 204 instead of 404 when provider not found (ignored `delete_provider` return value). Added 13 tests in test_sso_sqlalchemy_error.py covering all 8+3 routes for SQLAlchemyError→503 + IntegrityError→409 + DELETE 404. Created website docs stub. Status: partial.
- 2026-07-03: Cross-cutting QA (index 109). Added ProgrammingError→501 catches to 6 routes (create, update, delete, toggle, test, set/get group-mappings) and logging to all 7 catches including existing get_providers. Created test_sso_programming_error.py with 8 unit tests covering all 7 routes (list/create/update/delete/toggle/test/set-group-mappings/get-group-mappings). Updated product map: marked test-connection error-display [ ]→[x] (verified inline display works), added Error Handling section with 17 behaviour checkboxes, added test connection OIDC/SAML sections (12 checkboxes), added edge cases (empty state, loading, error alert, empty list). Updated Known Gaps: refined sidebar nav entry gap, renamed auth flow items to "v1 deferred", added ProgrammingError test coverage and BDD-admin-CRUD gaps. Status: partial (10 known gaps remain).

## Known Gaps
- **Delete provider does not warn about effect on active user sessions** — the confirmation dialog says only `This action cannot be undone`; it does not surface an SSO session-count/invalidation warning (not PRD-required; see Known Gaps)
- **No BDD scenarios for SSO provider admin UI** — BDD feature files exist only for the SSO auth flow (`sso_oidc.feature`, `sso_saml.feature`, `sso_team_mapping.feature`) but not for the admin provider CRUD operations (list/add/edit/delete/toggle/test). All admin UI testing is via unit tests.
- **No Pinia store** — all state is component-local in `SettingsSsoView.vue` (acceptable but less maintainable as feature grows).
- **Login page buttons** for configured OIDC/SAML providers do not exist — login flow is still password/Basic Auth; the SSO login endpoints (`/api/v1/auth/oidc/{provider}/login`, `/api/v1/auth/saml/login`) exist and work, but no buttons surface them on LoginView.vue.
- **Sidebar nav entry is tier-gated but not SSO-skill-gated** — the SSO nav item is correctly hidden for community users via `<<: *team` (required tier) and `required_roles: [admin]` in the manifest. However, it does not check the `sso` feature flag directly — a user on a team plan with an expired SSO license key still sees the link (the route itself will 402). The `FeatureGate show-disabled` UI on the page itself handles this gracefully (shows locked prompt), but the sidebar entry could show a lock icon.
- **Delete-provider confirmation does not warn about active SSO sessions** — the dialog says "This action cannot be undone" only; no warning about the effect on users who signed in through that provider (not PRD-required, minor UX gap).
- **No integration test for SAML real XML parsing** — unit tests mock the SAML parsing. No end-to-end test exercises real SAMLResponse XML against the actual XML parsing code path.
- **ProgrammingError test coverage uses patch pattern** — all 8 ProgrammingError tests simulate the error via `@patch` on the CRUD function. This tests the route-level catch but does not exercise the actual CRUD-level ProgrammingError scenario (no test forces ProgrammingError from within a CRUD call).
