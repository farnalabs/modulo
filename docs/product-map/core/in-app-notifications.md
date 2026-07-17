---
id: feat-core-in-app-notifications
prd: 8.11
delivery-tasks: []
bdd:
  - backend/tests/bdd/features/in_app_notifications/dashboard_panel.feature
  - backend/tests/bdd/features/in_app_notifications/dismiss_flow.feature
  - backend/tests/bdd/features/in_app_notifications/notification_filters.feature
  - backend/tests/bdd/features/in_app_notifications/sse_integration.feature
unit-tests: []
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
- [ ] `expires_at` not exposed in response (fixed per QA history)

## Security

- [x] Auth required for all notification endpoints
- [x] Notifications are user-scoped — users only see their own notifications
- [x] Admin delivery log requires operator role
- [ ] No rate limiting on SSE connections

## Known Gaps
