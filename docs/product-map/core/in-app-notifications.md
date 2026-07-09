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
