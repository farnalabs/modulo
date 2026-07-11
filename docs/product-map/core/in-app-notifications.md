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

## Known Gaps

- **PRD misalignment**: `prd: 8.11` references the outbound webhook notifications section but this feature covers in-app notifications (dashboard panel, dismiss/review-later, SSE, delivery log). The PRD has no dedicated section for in-app notifications.
- **All BDD steps are stubs**: All `given/when/then` step definitions in `test_in_app_notifications.py` are empty (`pass`). The 4 BDD feature files define 22 rich scenarios but none actually execute. Real implementations require a running backend + DB.
- **No unit tests**: `unit-tests: []` is accurate — there are no unit tests for the in-app notification routes or CRUD functions. Existing `test_notifications_endpoint.py` and `test_delivery_log.py` test the outbound webhook admin endpoints, not in-app notifications.
- **Notification preferences return 501**: `GET/PUT /preferences` both return 501 Not Implemented. The product map does not reference preferences.
- **SSE push not wired in frontend**: `NotificationsPage.vue` uses REST polling (`useDataFetch`), not SSE/EventBus subscription. The SSE BDD scenarios describe real-time refresh but the frontend doesn't implement it.
- **`expires_at` not exposed in response**: The `Notification` model has `expires_at` but `NotificationResponse` was missing the field (fixed in this QA).
