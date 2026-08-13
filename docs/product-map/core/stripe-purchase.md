---
id: feat-core-stripe-purchase
prd: 6.2,7.7
delivery-tasks: []
code:
  - backend/src/modulo/api/routes/stripe_webhook.py
  - backend/src/modulo/api/routes/admin_license.py
  - backend/src/modulo/core/stripe_fulfilment.py
  - backend/src/modulo/core/license_signing.py
  - backend/src/modulo/core/email_service.py
bdd:
  - backend/tests/bdd/features/licensing/license_management.feature
  - backend/tests/bdd/features/licensing/enterprise_gates.feature
unit-tests:
  - backend/tests/unit/api/test_stripe_webhook.py
  - backend/tests/unit/core/test_stripe_fulfilment.py
  - backend/tests/unit/core/test_license_signing.py
  - backend/tests/unit/api/test_admin_license.py
depends-on: [feat-core-feature-flag-ui]
status: partial
---

# Stripe Purchase Fulfilment

Enterprise licence purchase fulfilment. Stripe checkout for a paid subscription
signals `checkout.session.completed` / `invoice.paid` webhooks to
`POST /api/v1/webhooks/stripe`. The webhook verifies the `Stripe-Signature`
header (HMAC-SHA256 over the raw body with a 300s replay window) against the
`STRIPE_WEBHOOK_SECRET`, then schedules a background fulfilment task that signs
an Ed25519 enterprise licence key (`modulo_license_private_key`), provisions the
customer's org, and emails the licence to the buyer. Fails closed: any missing
or invalid signature returns 400 and never reaches fulfilment.

## Behaviours

- [x] Stripe webhook endpoint at `POST /api/v1/webhooks/stripe`
- [x] Signature verification against raw body with 300s replay window
- [x] Missing/invalid signature returns 400 without fulfilment
- [x] `checkout.session.completed` with unpaid subscription waits for `invoice.paid`
- [x] `invoice.paid` triggers licence signing + email fulfilment
- [x] Fulfilment runs as a FastAPI BackgroundTask (Stripe 2xx on receipt)
- [x] Idempotent fulfilment keyed on the Stripe event id
- [x] Webhook disabled (404) when Stripe is not configured
