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
signals `invoice.paid` (and `checkout.session.completed`) webhooks to
`POST /api/v1/webhooks/stripe`. The webhook verifies the `Stripe-Signature`
header (HMAC-SHA256 over the raw body with a 300s replay window) against the
`STRIPE_WEBHOOK_SECRET`, then schedules a background fulfilment task that signs
an Ed25519 enterprise licence key (`modulo_license_private_key`), provisions the
customer's org, and emails the licence to the buyer. Fails closed: any missing
or invalid signature returns 400 and never reaches fulfilment.

Fulfilment is triggered by `invoice.paid` ONLY — the authoritative "money
received" signal. For a card-paid subscription checkout Stripe sends BOTH
`checkout.session.completed` (with `payment_status=paid`) and `invoice.paid`
for the same purchase, so `checkout.session.completed` is treated as a pure ack
(200, no fulfilment) to avoid issuing two licences for one purchase.

## Behaviours

- [x] Stripe webhook endpoint at `POST /api/v1/webhooks/stripe`
- [x] Signature verification against raw body with 300s replay window
- [x] Missing/invalid signature returns 400 without fulfilment
- [x] `checkout.session.completed` is ack-only — never fulfils (prevents double licence on a single purchase)
- [x] `invoice.paid` triggers licence signing + email fulfilment
- [x] Fulfilment runs as a FastAPI BackgroundTask (Stripe 2xx on receipt)
- [x] Idempotent fulfilment keyed on the Stripe event id
- [x] Event id claimed only AFTER a successful licence email (failed email leaves the event unclaimed so Stripe retries re-attempt)
- [x] Webhook disabled (404) when Stripe is not configured
