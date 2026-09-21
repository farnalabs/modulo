---
id: feat-sso
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
  - backend/tests/bdd/features/auth/sso_admin_crud.feature
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
- **Delete-provider confirmation does not warn about active SSO sessions** — the dialog
  states only "This action cannot be undone".

## QA History
- 2026-09-21: **improve-architecture (product-map walk)** — closed the OIDC
  multi-org real-DB (RLS) integration gap (manifest `feat-sso` deferral). New
  `backend/tests/integration/auth/test_oidc_rls_resolution.py` mirrors the SAML
  RLS regression for the per-provider OIDC surface: the system role resolves an
  OIDC provider owned by a NON-first org, unknown slugs fail closed all-None
  (never RuntimeError→500), the app fallback resolves first-org-only inside a
  scoped transaction (FAR-1058 parity — `_resolve_oidc_provider` now opens its
  own `session.begin()` when the caller has none, matching
  `_resolve_saml_for_route`), an unbound app session sees zero OIDC providers,
  and `GET /oidc/{provider}/login` 307s cross-org with the resolved provider's
  client_id while an unknown slug is a 400 not a 500. Demoted the OIDC deferral
  and added the behaviour lines to `frontend/src/manifest.yaml`.
- 2026-09-21: **improve-architecture (product-map walk)** — closed the
  "No BDD scenarios for admin provider CRUD" gap. Registered
  `auth/sso_admin_crud.feature` into the executing BDD suite from the new
  `steps/test_sso_admin_crud.py`, driving the real `/api/v1/admin/sso` routes
  with only the DB CRUD, RLS and outbound-network seams patched — 16 scenarios:
  200 provider list with type badges (O/S), 201 OIDC create (client secret never
  echoed in the clear, computed callback URL) and SAML 2.0 create, the FAR-855
  unrestricted-provisioning 422 while the flag is off and duplicate-name 409,
  422 invalid provider type, 200 update / toggle (enabled=false), 400 empty
  update body, 204 delete, 404 on a missing provider for update/delete, the
  OIDC discovery-document and SAML metadata-XML connection tests
  (`_test_oidc_connection` / `_test_saml_connection` parse for real, only the
  pinned HTTP client patched), group-to-team mapping set/get, and the non-admin
  403. `_ORPHANED_BDD_FEATURES` stays empty.
- 2026-09-17: **improve-architecture (product-map walk)** — registered the
  `SsoProviderForm.vue` provider-form surface in the manifest `elements:`
  inventory for `/settings/sso`: the form ships the tenant-domain input
  (`sso-tenant-domain`) and the callback-URL copy control
  (`sso-callback-url-copy` + the `sso-callback-url-copied` copy-confirmation
  status). All three were rendered by `SettingsSsoView.vue` in the DOM while
  staying invisible to Remy's docs indexer / `/api/v1/manifest` (the same
  drift the 2026-09-12 JsonViewer registration closed). The component is now
  part of the route's reverse testid-coverage guard
  (`test_mapped_route_elements_cover_owning_view_testids`), so a newly shipped
  provider-form control can no longer silently drift out of the product map.

- 2026-09-12: **improve-architecture (product-map walk)** — registered the shared
  `JsonViewer` surface (`components/shared/JsonViewer.vue` static testids
  `json-viewer` / `json-viewer-{copy,expand-all,collapse-all,string-expand,string-collapse}`)
  in the manifest `elements:` inventory for `/settings/sso`: a successful connection
  test renders the provider info inline with `<JsonViewer :show-toolbar="true">`
  (`SettingsSsoView.vue`), so the viewer shipped in the DOM while staying invisible
  to Remy's docs indexer / `/api/v1/manifest`. The component is now part of the
  route's reverse testid-coverage guard
  (`test_mapped_route_elements_cover_owning_view_testids`).

- 2026-09-12: **improve-architecture (product-map walk)** — registered the shared
  plan-entitlement gate surface (`components/FeatureGate.vue` + `LockIcon.vue` static
  testids `feature-gate*` / `lock-icon`) in the manifest `elements:` inventory for `/settings/sso`
  and wired the two components into the reverse testid-coverage guard
  (`test_mapped_route_elements_cover_owning_view_testids`), so the entitlement-card
  surface on those pages stays visible to Remy's docs indexer / `/api/v1/manifest` and
  can no longer drift unguarded.

- 2026-09-11: **improve-architecture (product-map walk)** — extended the reverse
  testid-coverage guard (`test_mapped_route_elements_cover_owning_view_testids`) to
  `/settings/sso`: the whole-page view(s) `SettingsSsoView.vue` render static `data-testid`s that the
  product map `elements:` inventory already documents, but the surface was not yet
  guarded against drift. The route now maps to its owning view so a newly shipped
  testid can no longer silently stay invisible to Remy's docs indexer /
  `/api/v1/manifest`.

- 2026-08-25: **improve-architecture (product-map walk)** — shipped the login-page SSO
  provider buttons (``LoginView.vue`` consumes ``GET /api/v1/auth/sso/providers`` and
  renders OIDC/SAML buttons that link to the existing login endpoints). Coverage added in
  ``frontend/src/__tests__/LoginView.spec.ts`` (5 cases, incl. fails-closed on 402 / empty
  provider list / network error) and ``frontend/tests/e2e/login.spec.ts``. Unticked
  behaviour now verified; status: covered.
