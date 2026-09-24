---
id: feat-runtime
prd: N/A
adr: []
code:
  - backend/src/modulo/api/routes/admin_runtime_config.py
  - backend/src/modulo/api/routes/admin_rate_limits.py
  - backend/src/modulo/api/routes/admin_housekeeping.py
  - backend/src/modulo/api/routes/admin_run_retention.py
  - backend/src/modulo/api/middleware/rate_limiter.py
  - backend/src/modulo/api/routes/admin.py
  - backend/src/modulo/core/runtime_config/store.py
  - backend/src/modulo/core/runtime_config/key_bridge.py
unit-tests:
  - backend/tests/unit/api/test_admin_runtime_config.py
  - backend/tests/unit/api/test_admin_housekeeping.py
  - backend/tests/unit/api/test_admin_run_retention.py
  - backend/tests/unit/rate_limiter/test_admin_rate_limits_api.py
  - backend/tests/unit/core/test_housekeeping.py
  - backend/tests/unit/core/runtime_config/test_key_registry.py
  - backend/tests/unit/core/runtime_config/test_key_bridge.py
bdd:
  - backend/tests/bdd/features/admin/runtime-config.feature
  - backend/tests/bdd/features/admin/housekeeping.feature
  - backend/tests/bdd/features/operations/run_retention.feature
  - backend/tests/bdd/features/model_backends/rate_limiting.feature
  - backend/tests/bdd/features/rate_limiting/rate_limiting.feature
  - backend/tests/bdd/steps/test_rate_limiting.py
depends-on:
  - feat-system-config
status: covered
---

# Runtime Configuration

Runtime configuration, API rate limits, housekeeping cleanup, and run data
retention management. Admin surfaces for introspecting and overriding deployment
config, managing rate-limit rules, scanning/deleting orphaned entities, and
purging old run data.

## Behaviours

- [x] GET/PUT `/api/v1/admin/runtime-config` lists known config keys with
      current/default/env/override values and drift detection; override and
      clear are admin-only with unknown-key rejection; overrides are accepted
      only for hot-reloadable keys (each names its store-reading consumer)
      and rejected with a reason for boot-only keys, and clear is allowed for
      every known key (`backend/tests/bdd/features/admin/runtime-config.feature`,
      `backend/tests/unit/core/runtime_config/test_key_registry.py`)
- [x] POST `/api/v1/admin/runtime-config/reload` refreshes config from
      environment variables (`admin_runtime_config.py`)
- [x] GET/PUT `/api/v1/admin/rate-limits` returns and replaces rate-limit rules;
      mode (redis/in-memory) is reported
      (`admin_rate_limits.py`, `model_backends/rate_limiting.feature`)
- [x] The rate-limit middleware enforces independent per-endpoint budgets
      (`POST /api/v1/runs` 60/min, `POST /api/v1/triggers` 100/min, webhook
      flood 100/min), isolates counters per API key, applies the window reset,
      and returns a `Retry-After` header plus a rate-limit problem body on 429
      (`rate_limiting/rate_limiting.feature`)
- [x] Housekeeping scan returns cleanup candidates grouped by category;
      cleanup deletes selected candidates; checkpoint purge reclaims DB volume
      (`backend/tests/bdd/features/admin/housekeeping.feature`)
- [x] Run retention lists candidates with estimated byte sizes, exports as
      NDJSON, and purges terminal runs cascading to checkpoints; requires
      `confirm: true` (`run_retention.feature`)
- [x] Org sandbox concurrency is viewable/updatable on
      `GET/PUT /api/v1/admin/org/sandbox-concurrency` (clamped 1..100)
      (`admin.py`)

## Known Gaps

## QA History
- 2026-09-14: **product-map review pass** — closed the "no BDD for
  rate-limit middleware integration with specific endpoint types" gap: wired
  `rate_limiting/rate_limiting.feature` into the executing suite via
  `steps/test_rate_limiting.py` (8 scenarios across independent endpoint budgets,
  per-API-key counter isolation, window reset, Retry-After semantics, and the admin
  runtime rule update) and dropped the file from the tracked orphaned-BDD debt list
  (`_ORPHANED_BDD_FEATURES`). Two auth brute-force scenarios that duplicated
  `auth_brute_force.feature` were removed from the draft (that coverage already
  executes in `steps/test_auth_rate_limiting.py`). Rate-limit middleware behaviour is
  no longer unit-tested only.
- 2026-09-12: **product-map review pass** — registered the shared
  `PageHeader` right-slot surface (`components/shared/PageHeader.vue`, static testid
  `page-header-right`) in the manifest `elements:` inventory for
  `/admin/housekeeping` and `/admin/run-retention`, whose owning views
  (`AdminHousekeepingView.vue` / `AdminRunRetentionView.vue`) pass the header's
  `#right` action slot (hk-refresh / admin-run-retention-* header actions), and
  wired the component into the reverse testid-coverage guard
  (`test_mapped_route_elements_cover_owning_view_testids`) so the header action
  surface on both pages stays visible to Assistant's docs indexer / `/api/v1/manifest`.

- 2026-09-12: **product-map review pass** — registered the shared
  plan-entitlement gate surface (`components/FeatureGate.vue` + `LockIcon.vue` static
  testids `feature-gate*` / `lock-icon`) in the manifest `elements:` inventory for `/admin/housekeeping`, `/admin/run-retention`,
  `/admin/runners/concurrency`, `/admin/runners/profiles`,
  `/settings/rate-limits`, `/settings/runtime-config`
  and wired the two components into the reverse testid-coverage guard
  (`test_mapped_route_elements_cover_owning_view_testids`), so the entitlement-card
  surface on those pages stays visible to Assistant's docs indexer / `/api/v1/manifest` and
  can no longer drift unguarded.

- 2026-09-11: **product-map review pass** — extended the reverse
  testid-coverage guard (`test_mapped_route_elements_cover_owning_view_testids`) to
  `/settings/rate-limits` and `/settings/runtime-config`: the whole-page view(s)
  `SettingsRateLimitsView.vue` / `SettingsRuntimeConfigView.vue` render static `data-testid`s that the
  product map `elements:` inventory already documents, but the surface was not yet
  guarded against drift. The routes now map to their owning views so a newly shipped
  testid can no longer silently stay invisible to Assistant's docs indexer /
  `/api/v1/manifest`.

- 2026-09-11: **product-map review pass** — registered the
  app-layout DB-capacity banner (`DbCapacityBanner.vue` static testids
  `db-capacity-banner`, `db-capacity-usage`, `db-capacity-run-retention-link`,
  `db-capacity-housekeeping-link`) on the `/admin/housekeeping` and
  `/admin/run-retention` manifest `elements:` inventories — the banner renders
  organisation-wide via `AppLayout.vue` and its links land on those two pages,
  but its surface had no product-map home.
  `test_mapped_route_elements_cover_owning_view_testids` now maps both routes to
  their page views + `DbCapacityBanner.vue`, so the capacity banner cannot ship
  invisible to Assistant's docs indexer / `/api/v1/manifest`.
- 2026-09-11: **product-map review pass** — closed the
  `feat-runtime` element-inventory drift on the Runners concurrency tab: the
  effective-cap and preflight panels (`runner-concurrency-effective`,
  `runner-concurrency-preflight` in `RunnersConcurrencyTab.vue`) and the shared
  runner status strip (`runner-status-strip*` in `RunnerStatusStrip.vue`) that
  renders above both runner tabs are now registered on `/admin/runners/concurrency`;
  `test_mapped_route_elements_cover_owning_view_testids` now maps that route to
  the layout + tab + status-strip owning views so the surface can no longer ship
  invisible to Assistant's docs indexer / `/api/v1/manifest`.
- 2026-09-07: **product-map review pass** — added this
  behaviour-tracker for `feat-runtime`, which previously had no
  `docs/product-map/` entry. Behaviours verified against the runtime config,
  rate-limit, housekeeping, and run-retention routes and their test suites.
  Status: covered.
