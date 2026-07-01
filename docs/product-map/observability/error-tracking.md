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

# Error Tracking

Multi-source error ingestion pipeline with fingerprint-based grouping, alert rules,
external forwarders, and a frontend SDK. Supports backend (FastAPI middleware, Celery
hooks, logging handler), frontend (JS SDK with breadcrumbs), and external (Sentry,
Datadog, PagerDuty, Rollbar, OpsGenie, Loki) sources.

## Behaviours

### Data Model

- [x] `ErrorEvent` stores per-event data: fingerprint, level, message, stacktrace,
  context_json, source, environment, version, status
- [x] `ErrorGroup` groups events by unique `(organisation_id, fingerprint)` with
  aggregate count, level_peak, first_seen, last_seen, sample_event_id
- [x] `ErrorGroup.assigned_to` FK to accounts for ownership
- [x] `ErrorNotificationRule` stores per-org alert rules: name, enabled, condition
  fields, action_type (in_app/email/webhook), cooldown_seconds
- [x] `ErrorForwarderConfig` stores per-org, per-type forwarder configuration
- [x] DB-level CHECK constraints enforce valid level, source, status values
- [x] Append-only trigger on `error_events` prevents UPDATEs and DELETEs
- [x] Max 10 notification rules per org (3 for community/runner tier)

### Fingerprinting & Grouping

- [x] Fingerprint is SHA-256 hash of `message + normalized_stacktrace_top_5 + source`
- [x] Stacktrace normalization: keeps first 5 frames, strips `File "..."` line info
- [x] `upsert_error_group()` uses `SELECT ... FOR UPDATE` to prevent race conditions
- [x] On existing group: increments count, updates last_seen, promotes level_peak
- [x] Ingest result returns `{group_id, is_new}` per event
- [x] Group `count` is a lifetime counter — monotonically increasing

### Event Ingestion

- [x] `POST /api/v1/errors/ingest` accepts 1–20 events per request
- [x] HMAC-signed body via `X-Modulo-Error-Token` header
- [x] Validates level, source, message via Pydantic validators
- [x] Strips breadcrumbs from events before persisting
- [ ] Rate-limiting on ingest endpoint (documented but not enforced via middleware)

### Frontend SDK (JavaScript/TypeScript)

- [x] Singleton `ErrorTracker` auto-installs window error and unhandled rejection
  handlers
- [x] Vue plugin registers `app.config.errorHandler` and `app.config.warnHandler`
- [x] Manual `captureError(error, context?)` and `captureMessage(message, level)`
- [x] `window.__MODULO_ERROR_TRACKING_DISABLED__` disables all capture
- [x] BreadcrumbCollector: click, API call (fetch monkey-patch), route change
  breadcrumbs (max 50, ring buffer)
- [x] Context gatherer captures URL, viewport, userAgent, org plan info
- [ ] Breadcrumbs are sent in HTTP request but stripped by backend — not persisted

### Transport & Batching (Frontend)

- [x] In-memory queue flushes at 10 events or 5 seconds of inactivity
- [x] Rate limit: max 10 requests per 60-second sliding window
- [x] Retry with exponential backoff: 1s, 5s, 30s (max 3 retries)
- [x] 4xx responses cause batch to be dropped (configuration issue)
- [ ] Pending queue is in-memory only — lost on page reload

### HMAC Session Authentication

- [x] `POST /api/v1/errors/session-key` creates per-session HMAC key (1-hour TTL)
- [x] Ingest requires `X-Modulo-Error-Token` with HMAC-SHA256 of request body
- [x] Frontend uses `crypto.subtle.sign('HMAC', ...)` with SHA-256 digest fallback
- [x] Session key invalidated on auth change

### Celery Task Failure Hook

- [x] Registers as Celery `on_failure` handler for all tasks
- [x] Extracts task name, exception type+message, full stacktrace, args, kwargs
- [x] Runs in new asyncio event loop

### Backend Middleware & Logging

- [x] `CatchAllMiddleware` catches unhandled FastAPI route exceptions
- [x] Returns structured 500 JSON with `X-Request-ID`
- [x] `ErrorTrackingLogHandler` forwards ERROR+ log records to error tracking
- [x] Log handler silently drops records when `org_id` context var not set

### Error Dashboard API

- [x] `GET /api/v1/errors` — paginated list with filters: status, level, source,
  environment, search (message ILIKE)
- [x] `GET /api/v1/errors/{error_id}` — full detail with sample event and assignee
- [x] `PATCH /api/v1/errors/{error_id}` — update status and/or assignment
- [x] `GET /api/v1/errors/{error_id}/events` — paginated events in a group
- [x] Missing group returns 404 for all endpoints

### Admin Dashboard UI

- [x] `AdminErrorsView.vue` — filterable table with level badge, message, count,
  timestamps, status badge, assignee
- [x] Loading, error-with-retry, and empty states
- [x] `AdminErrorDetailView.vue` — summary cards, status actions (acknowledge/
  resolve/archive), assignment dropdown
- [x] Sample event display with expandable stacktrace and context JSON
- [x] Raw events paginated list
- [x] Loading, error, and empty states throughout

### Notification Rules

- [x] `GET /api/v1/errors/notification-rules` — paginated list (admin only)
- [x] `POST /api/v1/errors/notification-rules` — create rule with limit enforcement
- [x] `PUT /api/v1/errors/notification-rules/{rule_id}` — update rule
- [x] `DELETE /api/v1/errors/notification-rules/{rule_id}` — 204 No Content
- [x] Webhook URL validated to start with http:// or https://
- [ ] No user-facing notification rules UI exists — API-only
- [ ] `condition_window_seconds` stored on model but never evaluated in alert logic

### Alert Engine & Dispatch

- [x] Alert evaluation runs synchronously during ingest
- [x] Level match: exact match between rule condition_level and event level
- [x] Count threshold: group count >= condition_min_count
- [x] Cooldown check: per `(rule_id, group_id)` with Redis (or in-memory fallback)
- [x] in_app action creates NotificationDeliveryLog entry
- [ ] email action is a placeholder — only logs intent, no actual email sent
- [x] webhook action POSTs JSON to configured URL (15s timeout)
- [x] Slack detection formats message with emoji prefix

### External Forwarders

- [x] 6 forwarder types: Sentry, Datadog, PagerDuty, Rollbar, OpsGenie, Loki
- [x] `GET /api/v1/errors/forwarders` — list all 6 with status
- [x] `PUT /api/v1/errors/forwarders/{forwarder_type}` — create/update config
- [x] `POST /api/v1/errors/forwarders/{forwarder_type}/test` — test connection
- [x] Forwarders run independently; single failure does not block others
- [x] Base forwarder must return bool and never raise
- [ ] No forwarder configuration UI exists — API-only

### Prometheus / OTel Metrics

- [x] Counter `modulo_errors_total` per level/source/environment
- [x] Counter `modulo_error_alerts_total` on each alert dispatch
- [x] Graceful degradation: metrics silently disabled if OTel meter not configured
- [ ] `modulo_error_groups_active` gauge exists but is never updated

### BDD Coverage

- [ ] `backend/tests/bdd/features/error_tracking/error_ingestion.feature` — TODO
  placeholder
- [ ] `backend/tests/bdd/features/error_tracking/error_dashboard.feature` — TODO
  placeholder
- [ ] `backend/tests/bdd/features/error_tracking/error_notifications.feature` — TODO
  placeholder

### Known Gaps

- **Email dispatch placeholder:** `_dispatch_email()` only logs intent — no actual
  email integration
- **`condition_window_seconds` unused:** Stored on model and Pydantic schema but
  alert evaluation never applies a time-window filter
- **`modulo_error_groups_active` gauge never updated:** Metric function exists but
  is never called
- **`resolved_at` never set:** Column exists on `ErrorEvent` but no code populates
  it when a group is resolved
- **Breadcrumbs not persisted:** Frontend sends breadcrumbs (validated max 50) but
  backend strips them before DB insert
- **No notification rules UI:** API exists but no frontend views for managing rules
- **No forwarder configuration UI:** Only API CRUD — no frontend views
- **In-memory cooldown state lost on restart:** Without Redis, alert cooldown is
  in-memory only, not shared across processes
- **In-memory session keys non-persistent:** Without Redis, session keys are lost
  on restart
- **Rate-limiting on ingest not enforced:** API docs mention rate limiting but no
  middleware implements it
- **Append-only trigger conflicts with CASCADE delete:** On org deletion, cascade
  FK conflicts with append-only trigger on `error_events`
