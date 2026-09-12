---
id: feat-system-config
prd: N/A
adr: []
code:
  - backend/src/modulo/api/routes/admin_system_config.py
  - backend/src/modulo/db/models/system_config.py
unit-tests:
  - backend/tests/unit/api/test_admin_system_config.py
  - backend/tests/unit/db/test_system_config.py
bdd:
  - backend/tests/bdd/features/system_admin/system_admin_config.feature
depends-on: []
status: covered
---

# System Config

System-level configuration administration for deployment-wide `SystemConfig`
entries. System admins can list, create/update, and delete config key-value pairs
with sensitive value masking.

## Behaviours

- [x] GET `/api/v1/system-admin/config` lists all config entries with sensitive
      value masking; gated on `system.config.manage` (system admin only)
      (`admin_system_config.py`)
- [x] PUT `/api/v1/system-admin/config/{key}` creates or updates a config entry
      (upsert) with `updated_by` tracking
      (`backend/tests/bdd/features/system_admin/system_admin_config.feature`)
- [x] DELETE `/api/v1/system-admin/config/{key}` deletes a config entry; returns
      404 if not found
- [x] Regular admin receives 403 Forbidden on all system-config endpoints
      (`backend/tests/bdd/features/system_admin/system_admin_config.feature`)

## Known Gaps

- No BDD for DELETE system config; coverage is via unit tests.

## QA History
- 2026-09-12: **improve-architecture (product-map walk)** — registered the shared
  `JsonViewer` surface (`components/shared/JsonViewer.vue` static testids
  `json-viewer` / `json-viewer-{copy,expand-all,collapse-all,string-expand,string-collapse}`)
  in the manifest `elements:` inventory for `/admin/system/config`: each config
  entry's value renders inline with `<JsonViewer :show-toolbar="true">`
  (`AdminSystemConfigView.vue`), so the viewer shipped in the DOM while staying
  invisible to Remy's docs indexer / `/api/v1/manifest`. The component is now part
  of the route's reverse testid-coverage guard
  (`test_mapped_route_elements_cover_owning_view_testids`).

- 2026-09-12: **improve-architecture (product-map walk)** — registered the shared
  plan-entitlement gate surface (`components/FeatureGate.vue` + `LockIcon.vue` static
  testids `feature-gate*` / `lock-icon`) in the manifest `elements:` inventory for `/admin/system/config`
  and wired the two components into the reverse testid-coverage guard
  (`test_mapped_route_elements_cover_owning_view_testids`), so the entitlement-card
  surface on those pages stays visible to Remy's docs indexer / `/api/v1/manifest` and
  can no longer drift unguarded.

- 2026-09-07: **improve-architecture (product-map walk)** — added this
  behaviour-tracker for `feat-system-config`, which previously had no
  `docs/product-map/` entry. Behaviours verified against
  `routes/admin_system_config.py`, the system-config unit tests, and the
  `system_admin_config.feature` BDD. Status: covered.
