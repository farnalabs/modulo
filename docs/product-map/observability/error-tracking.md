---
id: feat-observability-error-tracking
prd: 8.25
delivery-tasks: [task-nv28-error-models, task-nv28-error-ingestion-api, task-nv28-error-backend-hooks, task-nv28-error-frontend-sdk, task-nv28-error-dashboard-ui, task-nv28-error-notification-engine, task-nv28-error-bdd-tests, task-nv28-error-product-map, task-nv29-error-sentry-connector, task-nv29-error-datadog-connector, task-nv29-error-pagerduty-connector, task-nv29-error-rollbar-opsgenie-loki, task-nv29-error-external-integrations-ui, task-nv29-error-external-bdd-tests]
bdd:
  - backend/tests/bdd/features/error_tracking/error_ingestion.feature
  - backend/tests/bdd/features/error_tracking/error_dashboard.feature
  - backend/tests/bdd/features/error_tracking/error_notifications.feature
code:
  - backend/src/modulo/db/models/error_event.py
  - backend/src/modulo/db/models/error_group.py
  - backend/src/modulo/db/models/error_notification_rule.py
  - backend/src/modulo/db/crud/error_tracking.py
  - backend/src/modulo/core/error_tracking/__init__.py
  - backend/src/modulo/core/error_tracking/alerting.py
  - backend/src/modulo/core/error_tracking/alert_dispatcher.py
  - backend/src/modulo/core/error_tracking/metrics.py
  - backend/src/modulo/core/error_tracking/celery_hooks.py
  - backend/src/modulo/api/routes/errors.py
  - backend/src/modulo/api/routes/error_notification_rules.py
  - backend/src/modulo/api/models/error.py
  - backend/src/modulo/api/models/error_notification_rule.py
  - backend/src/modulo/api/middleware/catch_all.py
  - backend/src/modulo/core/logging_config.py
  - frontend/src/lib/error-tracking/index.ts
  - frontend/src/lib/error-tracking/breadcrumbs.ts
  - frontend/src/lib/error-tracking/context.ts
  - frontend/src/lib/error-tracking/transport.ts
  - frontend/src/lib/error-tracking/types.ts
  - frontend/src/views/AdminErrorsView.vue
  - frontend/src/views/AdminErrorDetailView.vue
  - frontend/src/lib/api/errors.ts
depends-on: [feat-observability-otel-config-ui, feat-core-notifications]
unit-tests:
  - backend/tests/unit/error_tracking/test_error_models.py
  - backend/tests/unit/error_tracking/test_error_ingestion.py
  - backend/tests/unit/error_tracking/test_backend_hooks.py
  - backend/tests/unit/error_tracking/test_error_dashboard.py
  - backend/tests/unit/error_tracking/test_error_alerting.py
  - backend/tests/bdd/steps/test_error_tracking.py
  - frontend/src/__tests__/error-tracking.spec.ts
status: partial
---
