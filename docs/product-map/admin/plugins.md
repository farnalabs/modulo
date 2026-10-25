---
id: feat-plugins
prd: N/A
adr: []
code:
  - backend/src/modulo/api/routes/plugins.py
  - backend/src/modulo/core/plugin_registry
  - backend/src/modulo/core/plugin_registry/__init__.py
  - backend/src/modulo/util/__init__.py
unit-tests:
  - backend/tests/unit/api/test_plugin_registry_bdd.py
  - backend/tests/unit/plugin_registry/test_plugin_registry.py
  - backend/tests/unit/test_util.py
bdd:
  - backend/tests/bdd/features/plugins/plugin_registry.feature
depends-on:
  - feat-connectors
  - feat-model-backends
status: partial
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
      detail - broken entry points are never silently dropped from the registry
- [x] An entry point whose `load()` raises records `Failed to load entry point …`
      and is surfaced unhealthy through `health_check`
- [x] NO distribution metadata problem can abort discovery: `dist.name` /
      `dist.metadata` read and parse the distribution's `METADATA` file from
      disk, and a corrupt or half-written `.dist-info` (interrupted install,
      stripped image layer) makes that read raise. The read is now guarded, so
      the plugin is surfaced unhealthy under `Failed to read distribution
      metadata for plugin <id>`, remains visible in `list_plugins` /
      `health_check` / `entry_point_errors`, and **sibling plugins in the same
      scan are still discovered**. This matters because `discover_plugins` runs
      unguarded on the startup critical path (`api/main.py::_discover_plugins`),
      where an escaping raise made the whole instance unbootable because of one
      broken third-party package
      (`test_plugin_registry.py::test_unreadable_distribution_metadata_*`)
- [x] Unexpected failures from the entry-point provider or malformed entry-point
      objects are fail-soft: discovery logs the provider error and returns an
      empty discovery result, while a broken entry point is surfaced as an
      unhealthy `unknown-entry-point` record and healthy siblings continue
      scanning (`test_plugin_registry.py::test_discover_plugins_swallows_unexpected_entry_point_query_error`,
      `test_unreadable_entry_point_attributes_do_not_abort_discovery`)
- [x] Builders are only known to be *callable*, so `build_connector` /
      `build_model_backend` label a failing builder safely: a callable object, a
      `functools.partial` or any other `__name__`-less callable still yields the
      documented `RuntimeError("Connector builder for type … failed")` /
      `RuntimeError("Model backend builder for provider … failed")` **with the
      original exception attached as `__cause__`**, instead of the error handler
      itself raising an opaque `AttributeError` and discarding the cause. Such a
      builder works on the happy path too
      (`test_plugin_registry.py::test_*_without_dunder_name_*`,
      `test_callable_object_builder_still_works_on_the_happy_path`;
      `test_far1129_cap_plugins_registry.py` drives a REAL installed plugin whose
      entry point resolves to a callable-object builder - the state no in-repo
      unit test constructs, because every in-repo builder is a `def`)
- [x] The routes are gated behind `require_feature("plugin_management")` and
      `require_permission("plugin.list")`
- [x] The `plugin_management` feature gate FIRES: all three endpoints return 402
      when the feature is disabled on the plan (negative-path tests - the gate
      was previously declared but never observed to deny)
      (`backend/tests/unit/api/test_plugin_registry_bdd.py`)
- [x] The `/admin/plugins` view is READ-ONLY: it lists plugins and their health
      and offers no install / enable / disable / uninstall control, because
      lifecycle management is pip-managed and deliberately has no registry API
      (`frontend/src/views/AdminPluginsView.vue`,
      `frontend/src/__tests__/AdminPluginsView.spec.ts`; a toggle once shipped
      that called a non-existent `PUT /api/v1/plugins/{id}/toggle` and was
      removed)

## Known Gaps

- Plugin management (install / uninstall / upgrade) is done via pip and is not
  exposed through this API.
- The `/admin/plugins` UI is deferred from the MVP nav (private preview).

## QA History

- 2026-10-25: **Improve Architecture product-map walk** - found that the
  startup discovery guard handled common metadata exceptions but still allowed
  an unexpected entry-point provider failure, or an exception while reading an
  entry-point object's own `name` / `dist` attributes, to escape. Discovery now
  logs these failures, preserves healthy siblings, and records malformed
  entry-point objects under an explicit unhealthy placeholder id. Added focused
  regression tests for both failure shapes; lifecycle management remains an
  intentional pip-managed gap.

- 2026-10-14: **Improve Architecture product-map walk (index 400)** - two
  MAJOR findings fixed, both on the "a broken third-party plugin must never take
  the instance down" invariant the previous pass established. (1) MAJOR
  (error-path quality / Lens 9 List C - *implemented but unreachable in tested
  states*): `build_connector` and `build_model_backend` read `builder.__name__`
  **inside their own `except Exception` handlers**. Builders are only known to
  be callable - `register_connector_type` checks `callable(builder)` and an
  entry point can resolve to a callable object, a `functools.partial`, or a
  class instance, none of which define `__name__`. A failing such builder made
  the *handler itself* raise: the caller received an opaque
  `AttributeError: 'X' object has no attribute '__name__'` instead of the
  documented `RuntimeError`, and `__cause__` was `None` so the plugin's real
  exception was lost entirely. Now labelled with the new
  `modulo.util.callable_label`, which falls back to `type(x).__qualname__`;
  proved by unit tests AND by a real installed plugin whose entry point resolves
  to a callable-object builder. (2) MAJOR (resilience - closes the
  previously-deferred Known Gap below): `_load_entry_point` read
  `dist.name` / `dist.metadata` **outside** the guarded `ep.load()` block, so a
  distribution whose `METADATA` could not be read (a half-written or corrupt
  `.dist-info` from an interrupted install or a stripped image layer) raised
  straight out of `discover_plugins()` - aborting the whole scan with zero
  plugins discovered, and, because `_discover_plugins` runs unguarded in the ASGI
  lifespan, leaving the instance unbootable. Reproduced directly, then fixed:
  the metadata read is now guarded, the plugin is surfaced unhealthy under a
  descriptive detail, and sibling plugins keep discovering. Lens 8: extracted
  `.semgrep/callable-dunder-name.yml` (2 rules) and swept the whole codebase -
  it found 3 further instances of the same shape, all fixed
  (`pipeline_engine/decorator.py::cancellable_node`, `api/mcp_server.py`'s
  `_run_db_shell` log key, `launcher/doctor.py`'s crashing-check handler) plus
  the `type`-annotated false positives excluded. `src/` is clean under the new
  gate, and the gate is verified to flag the original `builder.__name__` file.

- 2026-10-12: **Improve Architecture product-map walk (index 397)** - three
  findings fixed. MAJOR (resilience): `PluginRegistry._load_entry_point` caught
  only `(ImportError, TypeError, AttributeError, SyntaxError)` from `ep.load()`;
  any other raise (a non-`ImportError` missing dependency, a module-scope config
  error, an `OSError`, …) escaped `discover_plugins()` on the startup critical
  path (`api/main.py::_discover_plugins`) and made the whole instance unbootable
  because of one broken plugin. The catch is now `Exception` (re-raising
  `asyncio.CancelledError`), so the plugin is surfaced unhealthy under the
  documented "Failed to load entry point …" detail; regression tests parametrise
  RuntimeError/ValueError/KeyError/OSError/LookupError and prove sibling plugins
  still discover (partial-failure isolation). MAJOR (frontend↔backend contract):
  `AdminPluginsView.vue` rendered an enable/disable toggle that called
  `PUT /api/v1/plugins/{id}/toggle` - a route that does not exist (the registry
  API is read-only by design, and the plugin unit test mocked only `api.GET`, so
  nothing exercised it). The dead control and its `activeStates` / `togglePlugin`
  machinery are removed; the status pill is now health-driven. MAJOR (coverage):
  the "[x] routes gated by `require_feature(\"plugin_management\")`" behaviour
  was declaration-only - added 402 tests for all three endpoints. MINOR
  (coverage): added the missing 500 fallback tests for the list and health
  endpoints. 90 plugin backend tests pass (79 → 90); frontend eslint / vue-tsc
  clean and `AdminPluginsView.spec.ts` passes (4 tests).

- 2026-09-29: **Improve Architecture product-map walk** - reconciled the
  tracker frontmatter `status:` with the manifest `feat-plugins` registry: the
  entry now reads `status: partial` (matching the manifest's unchecked
  "lifecycle management is not exposed through the registry API" deferral)
  instead of `covered`. The two layers previously disagreed on the same
  feature's coverage - a reader of the graph got the opposite answer from the
  machine layer Assistant reads from the manifest.

- 2026-09-26: **Improve Architecture product-map walk** - sharpened the manifest
  `feat-plugins` registry entry: the unchecked "lifecycle management is partially
  wired" gap now states exactly what remains - install / enable / disable / uninstall
  are NOT exposed through the registry API; the registry is read-only
  (importlib.metadata discovery at startup) and plugin installation/removal/upgrade
  is deliberately left to pip. The `/admin/plugins` UI remains a private_preview
  surface (FAR-544). Status stays `partial`.

- 2026-09-18: **product-map review pass** - closed the four
  `plugin_registry.feature` scenarios that were pinned `@awaiting-implementation`:
  added the missing `GET /api/v1/plugins/{plugin_id}` detail endpoint, changed
  the registry so an entry point with no package metadata is surfaced as an
  unhealthy plugin instead of being silently skipped, and wired the previously
  unwired step modules. The four scenarios now execute in CI
  (`PINNED_AWAITING_IMPLEMENTATION` no longer lists plugin_registry.feature).

- 2026-09-12: **product-map review pass** - registered the shared
  plan-entitlement gate surface (`components/FeatureGate.vue` + `LockIcon.vue` static
  testids `feature-gate*` / `lock-icon`) in the manifest `elements:` inventory for `/admin/plugins`
  and wired the two components into the reverse testid-coverage guard
  (`test_mapped_route_elements_cover_owning_view_testids`), so the entitlement-card
  surface on those pages stays visible to Assistant's docs indexer / `/api/v1/manifest` and
  can no longer drift unguarded.

- 2026-09-11: **product-map review pass** - extended the reverse
  testid-coverage guard (`test_mapped_route_elements_cover_owning_view_testids`) to
  `/admin/plugins`: the whole-page view(s) `AdminPluginsView.vue` render static `data-testid`s that the
  product map `elements:` inventory already documents, but the surface was not yet
  guarded against drift. The route now maps to its owning view so a newly shipped
  testid can no longer silently stay invisible to Assistant's docs indexer /
  `/api/v1/manifest`.
