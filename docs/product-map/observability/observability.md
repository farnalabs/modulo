---
id: feat-observability
prd: N/A
adr: []
code:
  - backend/src/modulo/api/routes/errors.py
  - backend/src/modulo/api/routes/observability.py
  - backend/src/modulo/api/routes/error_forwarder_config.py
  - backend/src/modulo/api/routes/admin_monitor_config.py
  - backend/src/modulo/api/routes/error_notification_rules.py
  - backend/src/modulo/core/error_tracking/
  - backend/src/modulo/otel_bridge/
  - backend/src/modulo/db/crud/error_tracking.py
  - backend/src/modulo/db/crud/observability.py
  - backend/src/modulo/db/models/error_event.py
  - backend/src/modulo/db/models/error_group.py
  - frontend/src/views/SettingsObservabilityView.vue
  - frontend/src/views/SettingsErrorForwardersView.vue
  - frontend/src/views/SettingsMonitorConfigView.vue
  - frontend/src/views/AdminErrorsView.vue
  - frontend/src/views/AdminErrorDetailView.vue
unit-tests:
  - backend/tests/unit/api/test_observability_routes.py
  - backend/tests/unit/api/test_error_forwarder_config.py
  - backend/tests/unit/api/test_admin_monitor_config.py
  - backend/tests/unit/api/test_error_notification_rules_route.py
  - backend/tests/unit/error_tracking/test_error_ingestion.py
  - backend/tests/unit/error_tracking/test_error_dashboard.py
  - backend/tests/unit/error_tracking/test_error_alerting.py
  - backend/tests/unit/error_tracking/test_error_metrics.py
  - backend/tests/unit/error_tracking/test_alert_dispatcher.py
  - backend/tests/unit/error_tracking/test_forwarders.py
  - backend/tests/unit/error_tracking/test_saq_hooks.py
bdd:
  - backend/tests/bdd/features/observability/metrics.feature
  - backend/tests/bdd/features/observability/error_forwarders.feature
  - backend/tests/bdd/features/observability/monitor_config.feature
  - backend/tests/bdd/features/observability/otel_traces.feature
  - backend/tests/bdd/features/errors/failed_state.feature
  - backend/tests/bdd/features/errors/retry.feature
  - backend/tests/bdd/features/error_tracking/error_dashboard.feature
  - backend/tests/bdd/features/error_tracking/error_ingestion.feature
  - backend/tests/bdd/features/error_tracking/error_notifications.feature
depends-on: []
status: covered
---

# Observability

Error tracking (ingestion, grouping, dashboard, alerting) plus infrastructure
observability exports (OTLP metrics/traces). Surfaces: `/admin/errors`,
`/admin/errors/:id`, `/settings/observability` (`feat-observability`).

_Error Forwarders (`/settings/error-forwarders`) and Browser Monitoring config
(`/settings/monitoring`) are deferred from the MVP nav (hidden via
`visibility: private_preview`). Behaviour detail removed for the MVP cut — restore
from git history when re-enabling. See FAR-547 (error forwarders) and FAR-543
(browser monitoring)._

## Behaviours

- [x] Error ingestion: backend errors are captured, events are ingestable via the
      public API (per-org session-key + HMAC), duplicates are deduplicated into
      groups by fingerprint, invalid events are rejected, batch ingestion accepts
      multiple events, and breadcrumbs persist inside `context_json`
      (`error_ingestion.feature`, `core/error_tracking.ErrorIngestionService`)
- [x] Error dashboard: list groups, filter by status, view group detail, resolve a
      group, and 404 on a missing group (`error_dashboard.feature`,
      `routes/errors.py` + `db/crud/error_tracking.py`)
- [x] Alerting + notification rules: a critical error fires an alert, a cooldown
      prevents alert storms, a condition window counts only recent events (with a
      lifetime-count fallback at window 0), and notification rules are
      configurable up to 10 per org (`error_notifications.feature`,
      `core/error_tracking/alerting.py`)
- _Error forwarders behaviour detail removed for the MVP cut (see the pointer above)._
- [x] OTLP observability export: `GET/PUT /api/v1/settings/observability`
      reads/updates the OTel endpoint + export interval (and the LangSmith key),
      serves stale cache on DB outage (degraded response, never hangs), masks
      sensitive headers, and a test endpoint validates an OTLP endpoint reachability
      (`metrics.feature`, `unit/api/test_observability_routes.py`)
- _Browser monitoring config behaviour detail removed for the MVP cut (see the pointer above)._
- [x] Frontend views behind the shipped routes render the settings and the admin
      error dashboard/detail surfaces (`SettingsObservabilityView.vue`,
      `AdminErrorsView.vue`, `AdminErrorDetailView.vue`)

## Known Gaps

- **`active_run_observability.feature` is deselected from CI** — the
  active-run observability contract (node-progress strip, queue banner, trigger
  actor, heartbeat, work items, child runs) is gated because the mock BDD client
  cannot drive the real run-detail/events routes; the shapes are unit-verified
  only.
- **No BDD for OTel *trace* span capture** — `otel_traces.feature` scenarios
  describe chain-span capture but run behind the OTel-exporter harness; the
  active metric/OTLP-config contracts are the BDD-locked surface.
- **Forwarder end-to-end delivery is not BDD-exercised per provider** — the
  forwarder config contract is locked; actual outbound delivery to each vendor
  is unit-tested at the dispatcher boundary.
- **No E2E browser-monitoring smoke test** — browser-monitor config is
  unit/BDD-verified at the API layer only.

## QA History

- 2026-08-27: **improve-architecture (product-map walk)** — added this entry to
  close the coverage gap for the registered `feat-observability` feature (no
  behaviour-tracker existed). Behaviours verified against `core/error_tracking/`,
  the `/api/v1/errors*` + `/api/v1/settings/observability` +
  `/api/v1/admin/monitor-config` routes, the error-observability BDD features,
  and the observability-forwarder-monitor unit suites. Status: covered.
- 2026-08-30: **duplicate-entry reconciliation** — a parallel product-map walk
  had added a second `feat-observability` tracker at `monitor/observability.md`,
  breaking the one-entry-per-feature invariant. This entry is retained; the
  duplicate's unique citations (`otel_bridge/`, the seven `error_tracking` unit
  suites, and the `otel_traces` / `errors/failed_state` / `errors/retry` BDD
  features) were folded in here. Status: covered.
