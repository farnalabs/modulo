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
  - backend/src/modulo/api/models/problem.py
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
  - frontend/src/lib/api/formatError.ts
unit-tests:
  - backend/tests/unit/api/test_observability_routes.py
  - backend/tests/unit/api/models/test_problem.py
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
  - backend/tests/unit/error_tracking/test_error_instance_scope.py
  - frontend/src/__tests__/AdminErrorsView.spec.ts
  - frontend/src/__tests__/AdminErrorDetailView.spec.ts
bdd:
  - backend/tests/bdd/features/observability/metrics.feature
  - backend/tests/bdd/features/observability/error_forwarders.feature
  - backend/tests/bdd/features/observability/monitor_config.feature
  - backend/tests/bdd/features/observability/otel_traces.feature
  - backend/tests/bdd/features/observability/active_run_observability.feature
  - backend/tests/bdd/features/errors/failed_state.feature
  - backend/tests/bdd/features/errors/recovery.feature
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
- [x] Two-partition read boundary (FAR-1547): error rows live in two partitions
      that never leak into each other. Every tenant read route is pinned to
      `principal.organisation_id`, so instance-level / unattributed rows (the
      public frontend ingest path and org-less backend ERRORs) written into the
      `SYSTEM_ORG_ID` sentinel partition were write-only. Three system-admin
      read routes now expose that partition — `GET /api/v1/errors/instance`,
      `/instance/{error_id}` and `/instance/{error_id}/events` — each gated by
      the system permission `errors.resolve_instance` as a route-level
      dependency (a tenant principal is refused 403 before any query runs, with
      or without a forged scope parameter) and each RLS-pinning its transaction
      to the sentinel org so the org-only policies pass. The instance read is
      deliberately read-only: there is no instance-scope PATCH. On the frontend,
      `/admin/errors` switches scope through a toggle rendered only for a
      system admin (`is_system_admin` claim) and a scope-aware detail view that
      renders the sentinel group read-only; a forged `?scope=instance` falls
      back to the tenant scope client-side (`routes/errors.py`,
      `AdminErrorsView.vue` / `AdminErrorDetailView.vue`,
      `unit/error_tracking/test_error_instance_scope.py`,
      `__tests__/AdminErrorsView.spec.ts` / `AdminErrorDetailView.spec.ts`;
      also recorded in the manifest `feat-observability` registry)
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
- [x] Active-run observability contract: run detail exposes `trigger_actor`,
      `heartbeat_at`, `capacity`, `work_item_refs` and `child_runs`, and the run
      event stream exposes `node_started` / `node_completed` / `node_failed`
      lifecycle events (`active_run_observability.feature`, `routes/runs.py` on
      `GET /api/v1/runs/{id}` / `GET /api/v1/runs/{id}/events`)
- [x] OTel *trace* span capture is BDD-exercised against the REAL
      `LangGraphOtelBridge` seams network-free and DB-free
      (`otel_traces.feature`): chain spans carry the org/pipeline
      `set_run_context` attribute stamps and appear for each node execution,
      tool callbacks become child spans under their parent node span (real
      parent/child wiring), connector callbacks stamp no credential fields in
      span attributes, and a telemetry-disabled provider (no span processor)
      exports no spans at all
- [x] HTTP errors carry route-specific RFC 9457 problem types (FAR-1545): the
      wire `type` becomes `urn:problem:modulo:<code>` for route error codes that
      name a genuinely distinct problem (`invalid_token`, `token_mismatch`,
      `already_configured`, `encryption_config_error`, `encryption_error`,
      `update_failed`) while the `code` extension member keeps the same value;
      the code→type map is deliberately sparse per RFC §4 (a plain resource 404
      and `database_error`/`conflict`/`internal_error`/`migration_required`
      stay status-derived — same type, so no minting), each type's `status`
      equals the HTTP status the raising route declares (§3.1.2) and carries a
      short per-type title (§3.1.3), and the frontend error formatter mirrors
      the per-type titles so a surfaced problem title matches the backend
      (`backend/src/modulo/api/models/problem.py`,
      `backend/tests/unit/api/models/test_problem.py`,
      `frontend/src/lib/api/formatError.ts`)

## Known Gaps

- **Forwarder end-to-end delivery is not BDD-exercised per provider** — the
  forwarder config contract is locked; actual outbound delivery to each vendor
  is unit-tested at the dispatcher boundary.
- **No E2E browser-monitoring smoke test** — browser-monitor config is
  unit/BDD-verified at the API layer only.

## QA History
- 2026-10-08: **Improve Architecture product-map walk** – closed the untracked
  FAR-1545 sub-surface (route-specific RFC 9457 problem types, merged in PR
  #1375): the specific `urn:problem:modulo:<code>` problem types shipped while
  neither the manifest `feat-observability` registry nor this tracker mentioned
  the error-envelope contract. Added the checked behaviour line plus the
  `api/models/problem.py` / `formatError.ts` / `test_problem.py` citations, and
  recorded the deliberate no-mint guards (status-derived generics) so the sparse
  code→type map is auditable.
- 2026-10-07: **FAR-1556 follow-up to FAR-1547 (PR #1353)** — registered the
  instance-errors surface the feature PR's allowlist excluded: the three
  system-admin sentinel-partition read routes (`GET /api/v1/errors/instance`,
  `/instance/{error_id}`, `/instance/{error_id}/events` in
  `backend/src/modulo/api/routes/errors.py`, already cited above), the
  `backend/tests/unit/error_tracking/test_error_instance_scope.py` suite and the
  two frontend scope-toggle spec files, plus a ticked behaviour bullet for the
  two-partition boundary. Behaviour and `status: covered` re-verified against
  `routes/errors.py`, the test suite and the manifest `feat-observability`
  registry (which already carries the matching FAR-1547 behaviour line and the
  `admin-errors-scope*` / `admin-error-detail-instance-readonly` testids) — no
  drift found, so the manifest needed no change.

- 2026-09-22: **product-map walk** — closed the "No BDD for OTel *trace* span
  capture" gap. Re-anchored `otel_traces.feature` so its four scenarios drive
  the REAL `LangGraphOtelBridge` seams network-free and DB-free (the
  InMemorySpanExporter pattern of `tests/unit/otel_bridge/test_handler.py`):
  run-root trace seeding via `start_run_root`, chain callbacks per node
  execution with the org/pipeline `set_run_context` stamps, a tool callback
  parented under its agent node span (child `parent.span_id` == parent
  `context.span_id`), connector chain callbacks whose attributes carry no
  credential fields, and a telemetry-disabled provider registered with NO span
  processor so nothing reaches the exporter. The previous steps fabricated span
  dicts in `ctx` and never touched the bridge.

- 2026-09-21: **product-map walk** — closed the "`active_run_observability.feature`
  is deselected from CI" gap. Un-gated the two scenarios and re-anchored them so
  they drive the REAL `GET /api/v1/runs/{id}` and `GET /api/v1/runs/{id}/events`
  routes with only the `_do_*` DB-fetch seams patched (the route handler, the
  `require_permission_any_credential` authz dependency, and `RunResponse` /
  `RunEventsResponse` serialization run for real). The event-stream scenario
  additionally drives the REAL per-run `RunEventBroker` in the shared registry,
  so `replay_since` and the node-lifecycle filter are asserted end to end.
  Removed the two scenarios from
  `PINNED_AWAITING_IMPLEMENTATION`; the feature is now executing BDD coverage.

- 2026-09-12: **product-map review pass** — registered the shared
  plan-entitlement gate surface (`components/FeatureGate.vue` + `LockIcon.vue` static
  testids `feature-gate*` / `lock-icon`) in the manifest `elements:` inventory for `/admin/errors`, `/admin/errors/:id`, `/settings/error-forwarders`,
  `/settings/monitoring`, `/settings/observability`
  and wired the two components into the reverse testid-coverage guard
  (`test_mapped_route_elements_cover_owning_view_testids`), so the entitlement-card
  surface on those pages stays visible to Assistant's docs indexer / `/api/v1/manifest` and
  can no longer drift unguarded. Also fixed the browser-monitoring page
  (`/settings/monitoring`): `SettingsMonitorConfigView.vue` opened `<FeatureGate>`
  without importing it, so the entitlement gate could not resolve and the whole
  page surface silently degraded to an unresolved custom element.

- 2026-09-12: **product-map review pass** — registered the shared
  surface the `/admin/errors/:id`, `/settings/error-forwarders` and
  `/settings/monitoring` whole-page views render (`components/shared/JsonViewer.vue`
  and `components/shared/ErrorAlert.vue` static testids `json-viewer*` /
  `error-alert-dismiss`) in the manifest `elements:` inventory for
  `/admin/errors/:id` and `/settings/error-forwarders`, and wired the three routes
  into the reverse testid-coverage guard
  (`test_mapped_route_elements_cover_owning_view_testids`) mapped to
  `AdminErrorDetailView.vue`, `SettingsErrorForwardersView.vue` and
  `SettingsMonitorConfigView.vue`, so the error-detail / forwarders / browser-monitor
  surfaces stay visible to Assistant's docs indexer and `/api/v1/manifest`.

- 2026-09-11: **product-map review pass** — registered the shared
  search-bar surface (`components/shared/FilterBar.vue` static testids
  `filter-bar-search` / `filter-bar-search-wrapper`) in the `/admin/errors` manifest
  `elements:` inventory and wired the component into the reverse testid-coverage
  guard, so the error-list search control the page ships stays visible to Assistant's
  docs indexer and `/api/v1/manifest`.

- 2026-09-11: **product-map review pass** — extended the reverse
  testid-coverage guard (`test_mapped_route_elements_cover_owning_view_testids`) to
  `/settings/observability`: the whole-page view(s) `SettingsObservabilityView.vue` render static `data-testid`s that the
  product map `elements:` inventory already documents, but the surface was not yet
  guarded against drift. The route now maps to its owning view so a newly shipped
  testid can no longer silently stay invisible to Assistant's docs indexer /
  `/api/v1/manifest`.

- 2026-08-27: **product-map review pass** — added this entry to
  close the coverage gap for the registered `feat-observability` feature (no
  behaviour-tracker existed). Behaviours verified against `core/error_tracking/`,
  the `/api/v1/errors*` + `/api/v1/settings/observability` +
  `/api/v1/admin/monitor-config` routes, the error-observability BDD features,
  and the observability-forwarder-monitor unit suites. Status: covered.
- 2026-08-30: **duplicate-entry reconciliation** — a parallel product-map walk
  had added a second `feat-observability` tracker at `monitor/observability.md`,
  breaking the one-entry-per-feature invariant. This entry is retained; the
  duplicate's unique citations (`otel_bridge/`, the seven `error_tracking` unit
  suites, and the `otel_traces` / `errors/failed_state` / `errors/recovery` BDD
  features) were folded in here. Status: covered.
