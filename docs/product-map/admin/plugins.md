---
id: feat-plugins
prd: N/A
adr: []
code:
  - backend/src/modulo/api/routes/plugins.py
  - backend/src/modulo/core/plugin_registry
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

_Deferred from the MVP nav (hidden via `visibility: private_preview`). Behaviour tracker removed for the MVP cut — restore from git history when re-enabling. See FAR-544._

## QA History

- 2026-09-11: **improve-architecture (product-map walk)** — extended the reverse
  testid-coverage guard (`test_mapped_route_elements_cover_owning_view_testids`) to
  `/admin/plugins`: the whole-page view(s) `AdminPluginsView.vue` render static `data-testid`s that the
  product map `elements:` inventory already documents, but the surface was not yet
  guarded against drift. The route now maps to its owning view so a newly shipped
  testid can no longer silently stay invisible to Remy's docs indexer /
  `/api/v1/manifest`.
