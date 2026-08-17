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
- [ ] All BDD steps are stubs — no runtime error path verification exists

## Edge Cases

- [x] Empty notification list returns empty response
- [x] Dismissing already-dismissed notification is idempotent
- [x] Dismiss all with no notifications is no-op
- [ ] SSE connection with no new notifications — no heartbeat mechanism
- [ ] Notification preferences return 501 — not implemented
- [x] `expires_at` exposed in response — verified 2026-08-15: `in_app_notifications.py:111` sets `expires_at=n.expires_at.isoformat() if n.expires_at else None` in the notification response schema (no dedicated unit assertion yet)

## Security

- [x] Auth required for all notification endpoints
- [x] Notifications are user-scoped — users only see their own notifications
- [x] Admin delivery log requires operator role
- [ ] No rate limiting on SSE connections

## Known Gaps

- **BDD steps are stubs — no runtime error-path verification** — the in-app-notifications BDD feature steps are stubs; SSE/runtime error paths are not end-to-end verified in Gherkin.
- **No SSE heartbeat** — an SSE connection with no new notifications sends no heartbeat/ping; idle connections rely on client-side reconnect only.
- **Notification preferences return 501 — not implemented** — no preferences endpoint exists.
- **No rate limiting on SSE connections** — an authenticated client can open unbounded SSE connections. Not PRD-mandated; a hardening item.

## QA History

- **2026-08-15 — distribute (final-pass sweep C)**: Verified `expires_at` is now exposed in the notification response (code `in_app_notifications.py:111`) and marked `[x]` (the old "not exposed" claim was stale). Documented the 4 remaining unchecked behaviours as genuine non-PRD gaps in the previously-empty Known Gaps section: stub BDD error paths, no SSE heartbeat, preferences endpoint 501 (not implemented), and no SSE rate limiting. No code changes — `in_app_notifications.py` is outside this sweep's scope. Status: partial.
