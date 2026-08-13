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
  - backend/src/modulo/core/error_tracking/saq_hooks.py
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
  - backend/tests/unit/error_tracking/test_error_metrics.py
  - backend/tests/bdd/steps/test_error_tracking.py
  - frontend/src/__tests__/error-tracking.spec.ts
status: partial
---

# Error Tracking

Multi-source error ingestion pipeline with fingerprint-based grouping, alert rules,
external forwarders, and a frontend SDK. Supports backend (FastAPI middleware, SAQ
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
- [x] Rate-limiting on ingest endpoint (10 req/min, enforced via RateLimitMiddleware)

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

### SAQ Job Failure Hook

- [x] `saq_hooks.after_process` ingests failed execute/resume jobs (source `saq`)
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
- [x] `condition_window_seconds` evaluated in alert logic — rule counts only events for the
  fingerprint created within the window; `0`/`None` falls back to the group lifetime count
  (`AlertEngine._count_events_in_window`, 6 unit tests + 3 BDD scenarios)

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
- [x] `modulo_error_groups_active` gauge — updated by `sample_error_group_metrics()`
  on the dispatcher_reconcile tick (every 60s, telemetry-enabled): counts
  non-terminal groups (`new`/`acknowledged`) per `level_peak`, explicitly zeroing
  levels with no active groups so a drained level never leaves a stale reading

### BDD Coverage

- [x] `backend/tests/bdd/features/error_tracking/error_ingestion.feature` — 5 real
  scenarios (backend error capture, API ingest, dedup, invalid input, batch)
- [x] `backend/tests/bdd/features/error_tracking/error_dashboard.feature` — 5 real
  scenarios (list, filter, detail, resolve, 404)
- [x] `backend/tests/bdd/features/error_tracking/error_notifications.feature` — 7 real
  scenarios (alert fires, cooldown, condition window x3, create rule, max rules)
- [x] `backend/tests/bdd/features/error_tracking/error_external_integrations.feature` — 6 real
  scenarios (list, configure Sentry, test connection, community gating,
  enable/disable toggle, unknown type 404)
- [ ] BDD step definitions are mock-based — no DB-level or integration-level coverage

### Error Handling

- [x] Missing `X-Modulo-Error-Token` header returns 401 with specific message
- [x] Invalid HMAC signature returns 401
- [x] Invalid JSON body returns 422
- [x] Pydantic validation errors return 422 with readable message
- [x] Non-existent org on principal returns 400 with specific message
- [x] Missing error group returns 404 with "Error group not found"
- [x] `update_error_group` ValueError propagates as 404
- [x] Missing notification rule returns 404 with "Notification rule not found"
- [x] Max rules exceeded returns 422 with limit in message
- [x] Non-admin on notification rules returns 403 with "Admin role required"
- [x] Non-admin on forwarder management returns 403 with "Admin role required"
- [x] Unknown forwarder type returns 404 with specific message
- [x] Forwarder connection test failure returns structured `{ok: false, message}` (not crash)
- [x] Missing DB table on any error-tracking route returns 501 with migration hint
- [x] Ingest route wraps entire DB transaction in ProgrammingError→501
- [x] Dashboard list/detail/update/events routes wrap DB in ProgrammingError→501
- [x] Notification rule CRUD routes wrap DB in ProgrammingError→501
- [x] Forwarder config CRUD + test routes wrap DB in ProgrammingError→501
- [x] All 13 DB-accessing error-tracking route handlers also catch SQLAlchemyError→503 (connection failures, deadlocks)
- [x] `resolved_at` set on error group when `update_error_group` sets status to `"resolved"`
- [x] Resolving a group cascades to its events — `new`/`acknowledged` events in the group become
  `resolved` with `resolved_at` populated (`_mark_group_events_resolved()`); already-resolved events
  keep their original timestamp and archived events are untouched
- [x] `POST /api/v1/errors/ingest` — catches Exception → 500
- [x] `POST /api/v1/errors/ingest/public` — catches Exception → 500
- [x] `GET /api/v1/errors` — catches Exception → 500
- [x] `GET /api/v1/errors/{error_id}` — catches Exception → 500
- [x] `PATCH /api/v1/errors/{error_id}` — catches Exception → 500
- [x] `GET /api/v1/errors/{error_id}/events` — catches Exception → 500
- [x] `GET /api/v1/errors/notification-rules` — catches Exception → 500
- [x] `POST /api/v1/errors/notification-rules` — catches Exception → 500
- [x] `PUT /api/v1/errors/notification-rules/{rule_id}` — catches Exception → 500
- [x] `DELETE /api/v1/errors/notification-rules/{rule_id}` — catches Exception → 500
- [x] `GET /api/v1/errors/forwarders` — catches Exception → 500
- [x] `PUT /api/v1/errors/forwarders/{forwarder_type}` — catches Exception → 500
- [x] `POST /api/v1/errors/forwarders/{forwarder_type}/test` — catches Exception → 500
- [x] All error-tracking error paths tested with 26 unit tests (ProgrammingError→501 + SQLAlchemyError→503 for all 3 router files)

### Known Gaps

- **Email dispatch placeholder:** `_dispatch_email()` only logs intent — no actual
  email integration
- ~~**`condition_window_seconds` unused:** alert evaluation never applied a time-window
  filter — **RESOLVED 2026-07-31**: `AlertEngine.evaluate()` now counts events per
  fingerprint within the window via `_count_events_in_window()` and compares that
  against `condition_min_count`; `0`/`None` falls back to the lifetime group count.
  Window-count query failures skip the rule (logged, fail-safe). 6 unit tests
  (`test_error_alerting.py::TestConditionWindow`) + 3 BDD scenarios
  (`error_notifications.feature`).~~
- **`modulo_error_groups_active` gauge never updated:** Metric function exists but
  is never called — ~~**RESOLVED 2026-08-12**: `sample_error_group_metrics()`
  counts non-terminal groups per `level_peak` and pushes the gauge on the
  dispatcher_reconcile tick (every 60s, telemetry-only); levels with zero active
  groups are set to 0 explicitly. 5 unit tests in `test_error_metrics.py`
  (`TestSampleErrorGroupMetrics`: per-level counts incl. resolved/archived
  exclusion, zero-level zeroing, failure swallowed, no-op without gauge, real
  SQLite end-to-end).~~
- **`ErrorEvent.resolved_at` never set when group resolved:** The column exists but no code populates
  it when a group's status changes to `"resolved"`. Only `ErrorGroup.resolved_at` is set.
  — ~~**RESOLVED 2026-08-12**: `update_error_group()` now cascades a group resolution to its events
  via `_mark_group_events_resolved()` — a bulk `UPDATE` flips `new`/`acknowledged` events in the
  group (same `organisation_id` + `fingerprint`) to `status='resolved'` with `resolved_at` set;
  already-terminal events (`resolved`/`archived`) keep their existing state so original resolution
  timestamps are preserved. 3 unit tests (`TestUpdateErrorGroup`: update-statement emitted on
  resolve, no bulk update on non-resolve statuses, real in-memory SQLite end-to-end verifying
  event status/resolved_at propagation and terminal-event preservation).~~
- **Breadcrumbs not persisted:** Frontend sends breadcrumbs (validated max 50) but
  backend strips them before DB insert
- **No notification rules UI:** API exists but no frontend views for managing rules
- **No forwarder configuration UI:** Only API CRUD — no frontend views
- **In-memory cooldown state lost on restart:** Without Redis, alert cooldown is
  in-memory only, not shared across processes
- **In-memory session keys non-persistent:** Without Redis, session keys are lost
  on restart
- **Append-only trigger conflicts with CASCADE delete:** On org deletion, cascade
  FK conflicts with append-only trigger on `error_events`
- **Public ingest daily cap memory leak (fixed 2026-07-07):** `_public_daily_event_count` entries for stale IPs are now pruned by `_prune_stale_ip_counters()`. See QA History 2026-07-07.

## QA History

### 2026-08-12 — improve-tests: QA lens pass on alert_dispatcher (45% → 100% line + branch coverage)

- **Coverage lifted to 100%** for `modulo.core.error_tracking.alert_dispatcher` via a
  dedicated 22-test hermetic suite (`tests/unit/error_tracking/test_alert_dispatcher.py`),
  replacing the thin partial coverage that previously lived inline in
  `test_error_alerting.py` / `test_alert_delivery_failed_metric.py`.
- **Dispatch routing matrix locked:** `in_app` (NotificationDeliveryLog entry with
  `_build_summary` from the sample event), `email` (SMTP resolution from org
  `settings_json` → global fallback → no-smtp disabled → no-admins → no-active-admins →
  send success/false → `EmailSendingError` failure metric → unexpected-error swallow),
  `webhook` (no-URL warning, Slack-emoji formatting, contract payload fields
  `alert_id`/`elevation_signal`/`attempt_n`/`run_group_id`, HTTP-error + request-error
  failure metrics), and unknown `action_type` warning.
- **`dispatch_alert_resolved` fully covered:** in-app record, webhook payload post,
  HTTP-error and request-error log paths — best-effort semantics verified (never raises).
- **Helpers pinned:** `_escape_html` and `_format_slack_payload` get direct assertions.
- **Verification:** 100% line + branch on `alert_dispatcher.py`; full
  `tests/unit/error_tracking` package (355 tests) passes; ruff check + format clean.

### 2026-08-12 — improve-architecture (event-resolution cascade gap→resolved)

- **Fixed feature gap:** `ErrorEvent.resolved_at` was never populated — resolving an
  error group (`update_error_group(status="resolved")`) set `ErrorGroup.resolved_at`
  but left every event in the group stuck at `status='new'` with no resolution
  timestamp, so the event-level lifecycle was permanently out of sync with the group.
  Implemented `_mark_group_events_resolved()` in `db/crud/error_tracking.py`: a single
  bulk `UPDATE` transitions the group's `new`/`acknowledged` events (matched by
  `organisation_id` + `fingerprint`) to `status='resolved'` with `resolved_at` set.
  Already-terminal events are excluded from the update — a previously-resolved event
  keeps its original `resolved_at` and archived events stay archived.
- **Test coverage:** added 3 unit tests in `test_error_models.py`
  (`TestUpdateErrorGroup`): (1) resolving a group emits an `UPDATE` against
  `error_events` setting `status` + `resolved_at`; (2) non-resolve statuses
  (`acknowledged`) never touch events; (3) a real in-memory SQLite end-to-end test —
  resolves a group over `new`/`acknowledged`/`resolved`/`archived` events and verifies
  the first two flip to resolved with a timestamp, the pre-resolved event keeps its
  original timestamp, and the archived event is untouched. 51/51 `test_error_models.py`
  + 336/336 error_tracking unit tests pass; ruff check + format clean; mypy --strict
  clean. Updated product map behaviour checkbox and known gaps.

### 2026-08-12 — improve-architecture (active-groups gauge gap→implemented)

- **Fixed feature gap:** `modulo_error_groups_active` was registered by
  `init_metrics()` and had a `set_active_groups()` helper, but nothing ever
  called it — the gauge stayed at its initial value forever. Implemented
  `sample_error_group_metrics(factory)` in `core/error_tracking/metrics.py`,
  mirroring the D1 run-runtime liveness sampler: it counts non-terminal error
  groups (`status IN ('new','acknowledged')`) per `level_peak` via a
  system-scoped session and pushes the gauge with a `level` label. Levels with
  zero active groups are explicitly set to 0 (a drained level can't leave a
  stale reading). Wired into the `dispatcher_reconcile` tick's telemetry block
  in `core/cron_helpers.py` (runs every 60s when `modulo_telemetry_enabled`),
  alongside `sample_run_runtime_metrics`.
- **Fail-safe semantics:** the sampler re-raises `asyncio.CancelledError` and
  swallows everything else (`metrics.sample_error_groups_failed`) — metrics can
  never break the reconcile tick.
- **Test coverage:** added 5 unit tests (`TestSampleErrorGroupMetrics` in
  `test_error_metrics.py`): per-level counts (resolved/archived excluded),
  zero-level zeroing, query-failure swallowed, no-op when the gauge handle is
  unset (no DB touch), and a real in-memory SQLite end-to-end test exercising
  the ORM query (status filter + GROUP BY). 37/37 `test_error_metrics.py` +
  281/281 error_tracking unit tests pass; ruff check + format clean; mypy
  --strict clean on src. Updated product map behaviour checkbox and known gaps.

- **Fixed feature gap:** `condition_window_seconds` on `ErrorNotificationRule` was stored,
  validated, and exposed by the API but never evaluated. Alert rules with
  `condition_min_count=N` fired off the group *lifetime* count, so "N events within the
  window" was silently ignored. Implemented `AlertEngine._count_events_in_window()`
  (`SELECT count(*) FROM error_events WHERE organisation_id = :org AND fingerprint = :fp
  AND created_at >= now() - window`). Rules with `window > 0` now compare the windowed
  count against `condition_min_count`; `0`/`None` falls back to the lifetime count.
- **Fail-safe semantics:** a window-count query failure logs `alert.window_count_failed`
  and skips only that rule — it cannot block the rest of the evaluation loop or the ingest.
- **Test coverage:** added 6 unit tests (`TestConditionWindow` in
  `test_error_alerting.py`): window threshold met/below, window=0 lifetime fallback,
  query-failure rule skip, per-rule independence, org+fingerprint scoping of the query.
  Added 3 BDD scenarios + step definitions to `error_notifications.feature`.
  222/222 error_tracking unit tests pass; 3/3 new BDD scenarios pass (7 pre-existing
  ingestion/dashboard BDD failures unchanged). Updated product map checkboxes and known gaps.

### 2026-07-07 — Cross-cutting QA (improve-architecture index 297)
- **CRITICAL**: Added `except Exception → 500` catches with `_log.exception` to all 13 DB-accessing error-tracking route handlers across errors.py (6 routes), error_notification_rules.py (4 routes), and error_forwarder_config.py (3 routes). Python-level errors (TypeError, KeyError, ValueError) previously propagated as opaque 500 to CatchAllMiddleware.
- **MAJOR**: Added `_prune_stale_ip_counters()` cleanup to `ingest_errors_public` to prevent unbounded memory growth from `_public_daily_event_count` dictionary.

### Cross-cutting QA (2026-07-05) — index 170 — feat-qa-error-tracking-170

**Critical fix — SQLAlchemyError catch → 503 on all 16 route handlers:**
All 16 error-tracking API route handlers across `errors.py` (10 handlers), `error_notification_rules.py` (4 handlers), and `error_forwarder_config.py` (4 handlers) previously only caught `ProgrammingError` → 501. Connection failures, deadlocks, and other `SQLAlchemyError` subclasses propagated as raw 500. Now each handler has a dual catch: `ProgrammingError` → 501 (missing migrations) and `SQLAlchemyError` → 503 (transient DB failure).

**Major fix — `resolved_at` now set on resolve:**
`update_error_group()` in `crud/error_tracking.py` previously never set `resolved_at` when status changed to `"resolved"`, despite the column existing on the `ErrorEvent` model. Now sets `group.resolved_at = datetime.now(UTC)` when `status == "resolved"`.

**Test coverage added:**
Created `test_error_programming_error.py` with 26 tests covering:
- 12 ProgrammingError→501 tests (6 errors endpoints, 4 rules endpoints, 3 forwarder endpoints — some tested twice for different routes)
- 12 SQLAlchemyError→503 tests (same coverage as above)
- 2 additional passes for ingest endpoints with HMAC verification mocked

**Product map updated:**
- Added 3 new [x] checkboxes to Error Handling (SQLAlchemyError→503, resolved_at, test coverage)
- Added this QA History section

### Cross-cutting QA (2026-07-03) — feat-qa-error-tracking-90

**Behaviour corrections:**
- Rate-limiting on ingest: was marked unchecked but rule exists in RateLimitMiddleware
  at `rate_limiter.py:72` — marked as [x]
- BDD coverage: was marked as "TODO placeholder" for all 3 files but all have real
  scenarios (15 total across 4 feature files) — marked as [x] with descriptions

**Code fixes applied:**
- Added `ProgrammingError` catch → 501 to all error-tracking API routes (errors.py,
  error_notification_rules.py, error_forwarder_config.py) — 15 route handlers
  protected against missing DB tables
- Forwarder test endpoint's second DB block (saving test result) was also missing
  `ProgrammingError` protection — now caught

**Known Gaps remaining (not fixed):**

| Gap | Reason |
|---|---|
| Email dispatch placeholder | Needs email provider integration — out of scope for this QA pass |
| `condition_window_seconds` unused | Would change alert engine logic — needs deliberate design |
| `modulo_error_groups_active` gauge never updated | Needs a scheduled job or hook after group mutations |
| `ErrorEvent.resolved_at` never set (ErrorGroup.resolved_at fixed in 2026-07-05 QA) | ErrorEvent requires model change + migration — partial fix done |
| Breadcrumbs not persisted | Backend strips them — requires model change + migration |
| No notification rules UI | Needs frontend views — new feature scope |
| No forwarder configuration UI | Needs frontend views — new feature scope |
| In-memory cooldown/session state | Requires Redis — documented, no code change needed |
| Append-only trigger / CASCADE conflict | Requires DB schema change — out of scope |
