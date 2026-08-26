---
id: feat-core-saml-integration
prd: 9.4
adr: []
code:
  - backend/src/modulo/auth/saml_handler.py
  - backend/src/modulo/auth/sso.py
unit-tests:
  - backend/tests/unit/auth/test_saml_parse_datetime.py
bdd:
  - backend/tests/bdd/features/auth/sso_saml.feature
  - backend/tests/bdd/steps/test_sso_saml.py
depends-on:
  - feat-auth-jwt-auth
status: covered
---

# SAML Integration

SAML 2.0 upstream integration via python3-saml: IdP metadata parsing,
`AuthnRequest` generation, and `SAMLResponse` parsing with full XML digital
signature verification, wired into the SSO ACS flow (`feat-auth-sso-provider-ui`).

## Behaviours

- [x] `AuthnRequest` generation from the IdP metadata (optional SP signing)
- [x] `SAMLResponse` parsing with XML digital signature verification against the
      IdP X.509 certificate from metadata
- [x] IdP metadata parsing via `OneLogin_Saml2_IdPMetadataParser`
- [x] SAML ACS flow validates the assertion, resolves the provider, and issues a
      JWT pair (`feat-auth-jwt-auth`)
- [x] Signed vs unsigned responses fail closed (no silent unsigned response accept)
- [x] Invalid / unparseable `SAMLResponse` surfaces a typed `SamlAuthError`

## Known Gaps

- **python3-saml version pinned by dependency audit** — upstream lib is vendored;
  behaviour is verified against the pinned version in the BDD suite.

## QA History

- 2026-08-25: **improve-architecture (product-map walk)** — entry added to close the
  dangling `depends-on: feat-core-saml-integration` edge in `auth/sso-provider-ui.md`.
  Behaviours re-verified against `auth/saml_handler.py`, `sso_saml.feature`, and unit
  tests. Status: covered.
