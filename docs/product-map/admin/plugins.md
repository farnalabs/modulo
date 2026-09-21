---
id: feat-plugins
prd: N/A
adr: []
code:
  - backend/src/modulo/api/routes/plugins.py
  - backend/src/modulo/core/plugin_registry
  - backend/src/modulo/core/plugin_registry/__init__.py
unit-tests:
  - backend/tests/unit/api/test_plugin_registry_bdd.py
  - backend/tests/unit/plugin_registry/test_plugin_registry.py
bdd:
  - backend/tests/bdd/features/plugins/plugin_registry.feature
depends-on:
  - feat-connectors
  - feat-model-backends
status: covered
---

# Plugin Registry

Read-only registry surface for third-party plugins discovered at startup via
standard `importlib.metadata` entry-point groups (`modulo.connectors`,
`modulo.model_backends`, `modulo.evals`, `modulo.schema_types`). Administrators
see installed plugins, their full manifests, and health status over
`/api/v1/plugins`; plugin install/uninstall/upgrade stays in pip. The admin UI
is deferred from the MVP nav (`visibility: private_preview`, FAR-544), but the
API surface ships and is gated by the `plugin_management` feature and the
`plugin.list` permission.

## Behaviours

- [x] Plugins are discovered at startup and available through the registry's
      `list_plugins` / `get_plugin` queries
      (`backend/src/modulo/core/plugin_registry/__init__.py`)
- [x] `GET /api/v1/plugins` lists every discovered plugin with
      `PLUGIN_ID`, `display_name`, `description`, `version`, `capabilities` and
      per-plugin health (patched-registry BDD + unit tests)
- [x] `GET /api/v1/plugins/{plugin_id}` returns the full manifest plus health
      for a single plugin, and 404s for an unknown id
- [x] `GET /api/v1/plugins/{plugin_id}/health` returns `{ok, detail, checked_at}`
      and 404s with `Plugin not found` for an unknown id
- [x] An entry point referencing a package with no installed metadata
      (`dist is None`) is surfaced as an unhealthy plugin with a descriptive
      detail — broken entry points are never silently dropped from the registry
- [x] An entry point whose `load()` raises records `Failed to load entry point …`
      and is surfaced unhealthy through `health_check`
- [x] The routes are gated behind `require_feature("plugin_management")` and
      `require_permission("plugin.list")`

## Known Gaps

- Plugin management (install / uninstall / upgrade) is done via pip and is not
  exposed through this API.
- The `/admin/plugins` UI is deferred from the MVP nav (private preview).

## QA History

- 2026-09-18: **product-map review pass** — closed the four
  `plugin_registry.feature` scenarios that were pinned `@awaiting-implementation`:
  added the missing `GET /api/v1/plugins/{plugin_id}` detail endpoint, changed
  the registry so an entry point with no package metadata is surfaced as an
  unhealthy plugin instead of being silently skipped, and wired the previously
  unwired step modules. The four scenarios now execute in CI
  (`PINNED_AWAITING_IMPLEMENTATION` no longer lists plugin_registry.feature).

- 2026-09-12: **product-map review pass** — registered the shared
  plan-entitlement gate surface (`components/FeatureGate.vue` + `LockIcon.vue` static
  testids `feature-gate*` / `lock-icon`) in the manifest `elements:` inventory for `/admin/plugins`
  and wired the two components into the reverse testid-coverage guard
  (`test_mapped_route_elements_cover_owning_view_testids`), so the entitlement-card
  surface on those pages stays visible to Remy's docs indexer / `/api/v1/manifest` and
  can no longer drift unguarded.

- 2026-09-11: **product-map review pass** — extended the reverse
  testid-coverage guard (`test_mapped_route_elements_cover_owning_view_testids`) to
  `/admin/plugins`: the whole-page view(s) `AdminPluginsView.vue` render static `data-testid`s that the
  product map `elements:` inventory already documents, but the surface was not yet
  guarded against drift. The route now maps to its owning view so a newly shipped
  testid can no longer silently stay invisible to Remy's docs indexer /
  `/api/v1/manifest`.
