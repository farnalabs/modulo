---
id: feat-core-saml-integration
prd: 9.4
adr: []
code:
  - backend/src/modulo/auth/saml_handler.py
  - backend/src/modulo/auth/sso.py
unit-tests:
  - backend/tests/unit/auth/test_saml_parse_datetime.py
  - backend/tests/unit/auth/test_saml_audience_containment.py
  - backend/tests/unit/auth/test_sso.py
  - backend/tests/integration/auth/test_saml_per_org_regression.py
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
signature verification, wired into the SSO ACS flow (`feat-sso`).

## Behaviours

- [x] `AuthnRequest` generation from the IdP metadata (optional SP signing)
- [x] `SAMLResponse` parsing with XML digital signature verification against the
      IdP X.509 certificate from metadata
- [x] IdP metadata parsing via `OneLogin_Saml2_IdPMetadataParser`
- [x] SAML ACS flow validates the assertion, resolves the provider, and issues a
      JWT pair (`feat-auth-jwt-auth`)
- [x] Signed vs unsigned responses fail closed (no silent unsigned response accept)
- [x] Invalid / unparseable `SAMLResponse` surfaces a typed `SamlAuthError`
- [x] Cross-org assertion reuse fails closed (FAR-1010): an assertion minted
      for one provider's entity ID, replayed at another provider's ACS, is
      rejected 401 ("SAML response validation failed") — on top of
      python3-saml's conditional audience/destination/recipient checks,
      `ModuloSamlAuth._enforce_audience_restriction` enforces audience
      containment itself (fail closed), and the Destination/Recipient
      validation derives from the real per-provider ACS URL rather than a
      hardcoded localhost (FAR-1011). Assertions are also validated against
      the per-provider entity ID recovered from the request URL/RelayState
      (`auth/saml_handler.py`, `test_saml_audience_containment`,
      `test_saml_per_org_regression`, `test_sso`)
- [x] Per-organisation provider isolation: metadata XML and the ACS surface
      are per-provider; an org's assertion is accepted only at the matching
      org's ACS (`test_saml_per_org_regression`)

## Known Gaps

- **python3-saml version pinned by dependency audit** — upstream lib is vendored;
  behaviour is verified against the pinned version in the BDD suite.

## QA History

- 2026-10-03: **Improve Architecture product-map walk** – closed the
  `feat-core-saml-integration` tracker lag left by FAR-1010 (cross-org
  assertion audience containment) and FAR-1011 (per-provider
  Destination/Recipient pinning): the manifest `feat-sso` registry carried
  the shipped "replayed to org B's ACS is rejected" behaviour, but the
  human-readable graph entry for the implementing surface
  (`auth/saml_handler.py`) had no behaviour line and cited only the
  datetime-parser unit suite. Added the checked behaviours — the fail-closed
  `_enforce_audience_restriction` containment on top of python3-saml's
  conditional checks, the real-ACS-URL Destination/Recipient pinning, and the
  per-provider ACS isolation — plus the `test_saml_audience_containment.py`,
  `test_saml_per_org_regression.py` and `test_sso.py` citations.
  `_ORPHANED_BDD_FEATURES` stays empty.

- 2026-08-25: **product-map review pass** — entry added to close the
  dangling `depends-on: feat-core-saml-integration` edge in `auth/sso-provider-ui.md`.
  Behaviours re-verified against `auth/saml_handler.py`, `sso_saml.feature`, and unit
  tests. Status: covered.
