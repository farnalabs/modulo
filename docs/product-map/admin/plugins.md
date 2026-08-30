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

Read-only admin surface (`/admin/plugins`, `GET /api/v1/plugins`) over the
installed-plugin inventory discovered from Python entry points
(`modulo.connectors`, `modulo.model_backends`, `modulo.plugins`) at startup.
Each plugin exposes a manifest (`PLUGIN_ID`, display name, description, version,
capabilities) and a live health status (import/load health, builder availability).
Plugin install/uninstall/upgrade is intentionally done via pip, not this API —
the registry only discovers and reports.

## Behaviours

- [x] `GET /api/v1/plugins` lists every discovered plugin with
      `PLUGIN_ID`/`display_name`/`description`/`version`/`capabilities` plus its
      `health_ok`/`health_detail`/`health_checked_at` (`test_plugin_registry_bdd.py`)
- [x] `GET /api/v1/plugins/{plugin_id}/health` returns per-plugin health and 404
      "Plugin not found" for unknown ids (empty registry 404s for any id)
- [x] Startup discovery scans `modulo.connectors`, `modulo.model_backends` and
      `modulo.plugins` entry points, skips entry points without a distribution,
      records per-entry-point load errors, and makes discovered plugins available via
      `list_plugins()` / `get_plugin()` (`core/plugin_registry/__init__.py`)
- [x] A discovered connector plugin registers its connector type capability; a model
      backend plugin registers its provider capability; each plugin's
      `capabilities` set reflects its registry membership
- [x] The registry builds connectors and model backends from plugin manifests,
      forwarding config/credentials with typed-error rejection, and wraps builder
      failures/cancellations instead of leaking raw errors
- [x] Health checking reports unhealthy when a plugin's distribution is missing from
      the environment or its metadata/import fails; cancellation never crashes the
      check (`test_plugin_registry.py`)

## Known Gaps

- **No per-plugin detail endpoint** — `GET /api/v1/plugins/{id}` is not exposed (only
  `/{id}/health` is); full-manifest retrieval is available at the registry level in
  tests, not over the wire.
- **`plugin_registry.feature` is stale** — several scenarios (discover, detail,
  startup discovery) carry `@awaiting-implementation` yet their behaviour is covered by
  `backend/tests/unit/api/test_plugin_registry_bdd.py`; the feature file is not a
  pytest-bdd-bound step file.
- **Management is out of scope by design** — install/uninstall/upgrade happen via pip;
  the feature graph tracks only discovery/reporting/health.
- **No PRD section reference** — plugin registry surfaces have no single PRD section
  mapped in code or ADRs.

## QA History

- 2026-08-28: **improve-architecture (product-map walk)** — added this behaviour-tracker
  for the registered manifest feature `feat-plugins`, which previously had no
  `docs/product-map/` entry. Behaviours verified against `api/routes/plugins.py`,
  `core/plugin_registry/__init__.py` and the plugin registry unit/BDD suites.
  Status: covered.