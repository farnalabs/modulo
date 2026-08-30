---
id: feat-notifications
prd: N/A
adr: []
code:
  - backend/src/modulo/core/notifier
  - backend/src/modulo/api/routes/admin_notifications.py
  - backend/src/modulo/api/routes/notifications.py
  - backend/src/modulo/api/routes/in_app_notifications.py
  - backend/src/modulo/core/email_service.py
  - backend/src/modulo/db/models/notification.py
  - backend/src/modulo/db/models/notification_delivery.py
  - backend/src/modulo/db/models/notification_endpoint.py
unit-tests:
  - backend/tests/unit/notifier/test_notifier.py
  - backend/tests/unit/notifier/test_event_mapper.py
  - backend/tests/unit/api/test_notifications_endpoint.py
  - backend/tests/unit/api/test_admin_notifications_webhooks.py
  - backend/tests/unit/api/test_in_app_notifications_preferences.py
  - backend/tests/unit/api/test_admin_email.py
bdd:
  - backend/tests/bdd/features/notifications/failure_webhook.feature
  - backend/tests/bdd/features/notifications/hitl_webhook.feature
  - backend/tests/bdd/features/notifications/signing.feature
  - backend/tests/bdd/features/in_app_notifications/dashboard_panel.feature
  - backend/tests/bdd/features/in_app_notifications/dismiss_flow.feature
  - backend/tests/bdd/features/in_app_notifications/notification_filters.feature
  - backend/tests/bdd/features/in_app_notifications/sse_integration.feature
  - backend/tests/bdd/steps/test_alpha_notifications.py
  - backend/tests/bdd/steps/test_in_app_notifications.py
depends-on:
  - feat-runs
  - feat-hitl
status: covered
---

# Notifications

Notifications surface pipeline events to operators across `/notifications`,
`/settings/email`, `/admin/notification-delivery` and the in-app panel. The
`core/notifier` dispatches outgoing webhooks with HMAC-SHA256 signing, bounded retry, and
dead-letter tracking of final failures; `api/routes/admin_notifications.py` exposes the
notification-delivery log with retry/DLQ admin actions; `event_mapper.py` maps run/HITL
events into typed notification payloads; and in-app notifications stream over SSE.

## Behaviours

- [x] A failure webhook fires on an unhandled node exception with the `run_id` and
      `error_detail`, includes the failed node name and error message, retries up to 3
      times, and auto-disables the endpoint after 10 consecutive failures (logging an
      alert) (`failure_webhook.feature`)
- [x] A HITL webhook fires when a run reaches an approval gate with `run_id`/`gate_id`
      and gate context, retries on failure, and after final failure the event lands on the
      dead-letter queue (`hitl_webhook.feature`)
- [x] Outgoing webhooks are signed with HMAC-SHA256 (per-endpoint secrets) and carry
      `X-Modulo-Signature` / `X-Modulo-Timestamp`, and a receiver can verify the signature
      (`signing.feature`)
- [x] Delivery attempts are recorded against `NotificationDeliveryLog` and are
      manageable from `/admin/notification-delivery` (status/event filters, retry of
      failed deliveries) (`api/routes/admin_notifications.py`,
      `test_admin_notifications_webhooks.py`)
- [x] In-app notifications stream over SSE with a dashboard panel, per-user filters and a
      dismiss flow (`in_app_notifications/*.feature`, `test_in_app_notifications.py`)
- [x] Email delivery settings and message sending are configured under `/settings/email`
      (`core/email_service.py`, `test_admin_email.py`)

## Known Gaps

- **In-app notification preference defaults and digest batching** are not surfaced here;
      the SSE panel is the primary in-app surface cited.
- **Webhook payload schemas are not versioned** — receivers depend on the documented
      field set (run_id / error_detail / gate context); there is no webhook payload
      version negotiation.

## QA History

- 2026-08-27: **improve-architecture (product-map walk)** — added this behaviour-tracker
  for the registered manifest feature `feat-notifications`, which previously had no
  `docs/product-map/` entry. Behaviours verified against `core/notifier`,
  `api/routes/admin_notifications.py` and the notifications BDD/unit suites.
  Status: covered.
