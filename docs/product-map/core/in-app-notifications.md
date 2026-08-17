---
id: feat-core-in-app-notifications
prd: 8.11
delivery-tasks: []
bdd:
  - backend/tests/bdd/features/in_app_notifications/dashboard_panel.feature
  - backend/tests/bdd/features/in_app_notifications/dismiss_flow.feature
  - backend/tests/bdd/features/in_app_notifications/notification_filters.feature
  - backend/tests/bdd/features/in_app_notifications/sse_integration.feature
unit-tests:
  - backend/tests/unit/api/test_notifications_endpoint.py
  - backend/tests/unit/db/test_notification_endpoint_json.py
  - backend/tests/unit/notifier/test_notification_endpoints_api.py
code:
  - backend/src/modulo/api/routes/in_app_notifications.py
  - backend/src/modulo/db/models/notification.py
  - backend/src/modulo/db/crud/notifications.py
  - frontend/src/views/NotificationsPage.vue
  - frontend/src/views/AdminNotificationDeliveryLogView.vue
depends-on: [feat-core-notifications]
status: partial
---

# In-App Notifications

In-app notification panel, SSE streaming, dashboard panel, dismiss/review-later flows.

## Behaviours

### Dashboard Panel

- [x] Top 5 notifications on dashboard panel
- [x] Unread count badge
- [x] Full listing with filters (type, read status, date range)

### Dismiss/Review Later

- [x] Dismiss individual notifications (user scope)
- [x] Dismiss all notifications (org scope)
- [x] Review later flow

### SSE Streaming

- [x] Real-time notification delivery via SSE
- [x] SSE integration test coverage

### Notification Delivery Log (Admin)

- [x] Admin view for delivery log
- [x] Status tracking per notification

## Error Handling

- [x] Notification CRUD routes catch `ProgrammingError` → 501
- [x] Notification CRUD routes catch `SQLAlchemyError` → 503
- [x] Notification CRUD routes catch `Exception` → 500 with logging
- [x] Missing notification ID returns 404
- [x] SSE connection errors handled gracefully — client reconnects
- All BDD steps are stubs — no runtime error path verification exists (see Known Gaps)

## Edge Cases

- [x] Empty notification list returns empty response
- [x] Dismissing already-dismissed notification is idempotent
- [x] Dismiss all with no notifications is no-op
- SSE connection with no new notifications — no heartbeat mechanism (see Known Gaps)
- Notification preferences return 501 — not implemented (see Known Gaps)
- [x] `expires_at` exposed in the response — `NotificationResponse.expires_at` is set from `n.expires_at.isoformat()` in the list endpoint (`in_app_notifications.py:111`)

## Security

- [x] Auth required for all notification endpoints
- [x] Notifications are user-scoped — users only see their own notifications
- [x] Admin delivery log requires operator role
- No rate limiting on SSE connections (see Known Gaps)

## Known Gaps

- Notification preferences (`GET`/`PUT /api/v1/notifications/preferences`) return 501 "Notification preferences are not yet implemented." — the response/update schemas exist but the feature is not wired.
- SSE connections have no heartbeat mechanism — a connection with no new notifications receives no keep-alive, so intermediaries may time it out.
- No rate limiting on SSE connections — a client can hold many connections unthrottled.
- BDD step definitions in `tests/bdd/steps/test_in_app_notifications.py` are all stubs (docstring: "All steps are stubs — they accept and ignore all arguments") — the 4 `in_app_notifications/` feature files have no runtime verification (unit tests in `test_notifications_endpoint.py` are the real coverage).

## QA History

### 2026-08-15 — coverage sweep (partial-small-a)

- **Marked `expires_at` [x]** — the checkbox was stale; `NotificationResponse` carries `expires_at` (`in_app_notifications.py:62,111`), so it IS exposed in the list response. **Confirmed the other unchecked items are genuine gaps and documented them in a new Known Gaps section**: preferences endpoints return 501, SSE has no heartbeat, no SSE rate limiting, and the BDD steps are all stubs (verified — `test_in_app_notifications.py` step bodies are `pass`). Status: partial (21/26 → 22/26).

## Known Gaps
