---
id: feat-auth-sso-provider-ui
prd: 9.4
delivery-tasks: []
code:
  - frontend/src/views/SettingsSsoView.vue
  - frontend/src/views/LoginView.vue
  - frontend/src/components/SsoProviderForm.vue
  - frontend/src/router/index.ts
  - frontend/src/config/navigation.ts
  - frontend/tests/e2e/login.spec.ts
  - backend/src/modulo/api/routes/admin_sso.py
  - backend/src/modulo/api/routes/sso.py
  - backend/src/modulo/auth/sso.py
  - backend/src/modulo/db/crud/sso_provider.py
  - backend/src/modulo/db/models/sso_provider.py
unit-tests:
  - backend/tests/unit/api/test_admin_sso.py
  - backend/tests/unit/api/test_sso_gating.py
  - backend/tests/unit/api/test_error_handling.py
  - backend/tests/unit/auth/test_sso.py
  - frontend/src/__tests__/LoginView.spec.ts
bdd:
  - backend/tests/bdd/features/auth/sso_oidc.feature
  - backend/tests/bdd/features/auth/sso_saml.feature
  - backend/tests/bdd/features/auth/sso_team_mapping.feature
depends-on:
  - feat-core-oidc-integration
  - feat-core-saml-integration
  - feat-core-ssrf
  - feat-teams-org-entity
status: covered
---

# SSO Provider UI

Admin settings page (`/settings/sso`, `settings-sso`) for configuring OIDC and SAML 2.0
identity providers. Team-gated (§9.4): the route is `required_tier: team` +
`required_roles: [admin]` in `frontend/src/manifest.yaml`. Referenced by the
incident-response playbook as the prevention control for IdP-initiated SSO validation
(`docs/security/incident-response-playbook.md`).

## Behaviours

- [x] Admin can view the list of all configured SSO providers with type badges (O / S)
- [x] Admin can add an OIDC provider (client ID, client secret, discovery URL, scopes)
- [x] Admin can add a SAML 2.0 provider (metadata URL, metadata XML, entity ID)
- [x] Provider form shows conditional fields based on selected type (OIDC vs SAML)
- [x] Common fields per provider: name, auto-provision toggle, default role
      (operator/runner), group-to-team mappings
- [x] Admin can edit, enable/disable, and delete an SSO provider (confirmation dialog)
- [x] Admin can test an SSO provider connection — OIDC resolves the discovery URL,
      SAML parses the metadata XML
- [x] Adds/edits/deletes/toggles raise audit events
- [x] SSO provider management is admin-only (403 for non-admin)
- [x] Duplicate provider name → 409 (with FOR UPDATE lock); empty update body → 400;
      invalid provider type/default role → 422; provider not found → 404
- [x] ProgrammingError → 501 and SQLAlchemyError → 503 on the admin CRUD routes
- [x] OIDC callback exchanges the code, verifies HMAC-signed state (CSRF), and issues a
      JWT pair; SAML ACS parses `SAMLResponse`, validates the assertion, and issues JWTs
- [x] JIT provisioning creates the user with the provider's default role; group
      mappings apply at provisioning time
- [x] Configured SSO providers surface on the login page as buttons — `LoginView.vue`
      calls `GET /api/v1/auth/sso/providers` on mount and renders an OIDC button per
      advertised provider (linking to `/api/v1/auth/oidc/{provider}/login`) plus a SAML
      button when SAML is enabled; when the feature is unavailable (402) or no provider
      is advertised, the page stays on password login (fails closed)

## Known Gaps

- **Sidebar entry tier-gated but not SSO-skill-gated** — the nav entry hides for
  community (team tier required) but does not re-check the `sso` license key; the page
  renders a locked prompt via `FeatureGate show-disabled`.
- **No BDD scenarios for admin provider CRUD** — BDD covers the auth flows only; admin CRUD
  is unit-tested.
- **Delete-provider confirmation does not warn about active SSO sessions** — the dialog
  states only "This action cannot be undone".

## QA History

- 2026-08-25: **improve-architecture (product-map walk)** — shipped the login-page SSO
  provider buttons (``LoginView.vue`` consumes ``GET /api/v1/auth/sso/providers`` and
  renders OIDC/SAML buttons that link to the existing login endpoints). Coverage added in
  ``frontend/src/__tests__/LoginView.spec.ts`` (5 cases, incl. fails-closed on 402 / empty
  provider list / network error) and ``frontend/tests/e2e/login.spec.ts``. Unticked
  behaviour now verified; status: covered.
