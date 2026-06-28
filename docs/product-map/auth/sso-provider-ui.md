---
id: feat-auth-sso-provider-ui
prd: 9.4
delivery-tasks: [task-nv6-sso-provider-ui]
bdd:
code:
  - frontend/src/views/SettingsSsoView.vue
  - frontend/src/components/SsoProviderForm.vue
  - frontend/src/router/index.ts
  - backend/src/modulo/api/routes/admin_sso.py
  - backend/src/modulo/api/routes/sso.py
  - backend/src/modulo/auth/sso.py
depends-on: [feat-core-oidc-integration, feat-core-saml-integration]
status: partial
---
# SSO Provider UI Admin settings page for configuring OIDC and SAML 2.0 identity providers. Enterprise-gated feature (9.4, license key feature `sso`). ## Behaviours ### Provider list and management
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
- [x] SSO provider management is admin-only (403 for non-admin users) ### Enterprise gating
- [ ] SSO settings page is hidden or locked behind enterprise license check
- [ ] Free-tier users see lock icon / upgrade prompt on SSO settings nav entry
- [ ] SAML routes return 402/403 when license lacks `sso` feature flag ### Edge cases and error states
- [ ] Add provider with empty required fields shows inline validation errors
- [ ] Duplicate provider name is rejected
- [ ] Test connection on OIDC provider with invalid/unreachable discovery URL shows error inline
- [ ] Test connection on SAML provider with invalid/empty metadata shows error inline
- [ ] Delete provider warns about effect on active user sessions
- [ ] SAML endpoints return 403 when enterprise license is absent
- [ ] Form state is preserved on validation failure (no page loss)
- [ ] Test connection results show success or failure details inline (no page navigation) ### Authentication flow integration
- [ ] Configured OIDC providers appear on the login page as buttons
- [ ] SAML SSO login is available when SAML is enabled + licensed
- [ ] JIT provisioning creates user on first SSO login with configured default role
- [ ] Group-to-team mapping from `SsoProvider.group_mappings` applies at JIT provisioning
- [ ] OIDC callback exchanges auth code, verifies state, issues JWT
- [ ] SAML ACS parses `SAMLResponse`, validates assertion, issues JWT ## Known Gaps - **No BDD feature files** for SSO provider UI (`backend/tests/bdd/features/auth/` has `login.feature`, `api_keys.feature`, `rbac.feature`, `tenant_isolation.feature` — none for SSO)
- **No Pinia store** — all state is component-local in `SettingsSsoView.vue` (acceptable but less maintainable as feature grows)
- **Login page buttons** for configured OIDC/SAML providers may not exist yet — login flow is still Basic Auth (alpha); v1 SSO login UI is not implemented
- **`depends-on` references task IDs** (`task-nv6-*`) rather than feature IDs — should reference `feat-nv6-oidc-integration` and `feat-nv6-saml-integration` once those feature map entries exist 