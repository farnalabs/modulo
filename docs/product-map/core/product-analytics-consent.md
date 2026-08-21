---
id: feat-product-analytics-consent
prd: 8.32
delivery-tasks: []
bdd: []
code:
  - backend/src/modulo/api/routes/product_analytics.py
  - backend/src/modulo/core/product_analytics/
unit-tests:
  - backend/tests/unit/product_analytics/test_consent.py
depends-on: []
status: partial
---

# Product Analytics Consent & Settings (FAR-354)

Opt-in product analytics consent model and instance-level analytics switch.
Orgs are prompted to accept/decline/dismiss product analytics egress; an
instance-level flag (controlled by an admin) gates whether any egress is
allowed. Consent state lives in `Organisation.settings_json` under the
`product_analytics` block.

## Behaviours

### API — Consent (`POST /api/v1/org/product-analytics/consent`)
- [x] Accept / decline / dismiss actions update the `product_analytics` block in `settings_json`
- [x] Prompt-eligibility gate: ineligible orgs receive 409 (dismissed prompts re-appear after 7 days)
- [x] Audit event recorded for each consent action

### API — Read state (`GET /api/v1/org/product-analytics`)
- [x] Returns current consent level, prompt state, instance switch, and egress-allowed flag

### API — Level update (`PUT /api/v1/org/product-analytics`)
- [x] Admin-only toggle (`org.settings.update` permission) sets level off/all
- [x] Audit event recorded for each level change

### Core — Instance switch & egress
- [x] `is_instance_analytics_enabled` reads the instance flag
- [x] `is_egress_allowed` computes egress from instance flag + org level
