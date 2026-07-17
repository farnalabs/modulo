---
id: feat-core-shared-manifest
prd: 8.28
delivery-tasks: []
bdd: []
unit-tests:
  - backend/tests/unit/remy/test_manifest.py
code:
  - backend/src/modulo/api/routes/manifest.py
  - backend/src/modulo/core/manifest.py
  - frontend/src/manifest.yaml
depends-on: [feat-core-navigation-restructure]
status: partial
---

# Core Shared Manifest

A single `frontend/src/manifest.yaml` file serving as the source of truth for page routes, interactive elements, sidebar groups, and their relationships to the product map, i18n, permissions, and feature tiers.

## Behaviours

- [x] Schema v1 with route entries defining name, testid, breadcrumb, sidebar group/order
- [x] i18n key and tier/permission requirements per route
- [x] YAML anchors for community/team tier sharing
- [x] Deprecated flag support
- [ ] Backend consumption of manifest for route permission validation
- [ ] Auto-generation from manifest to product map entries

## Error Handling

- [x] `GET /api/v1/manifest` catches `ProgrammingError` → 501 with migration hint
- [x] `GET /api/v1/manifest` catches `SQLAlchemyError` → 503
- [x] `GET /api/v1/manifest` catches `Exception` → 500 with `logger.exception`
- [x] Frontend manifest YAML parse failure caught during build — build fails with `vue-tsc` type errors

## Edge Cases

- [x] Empty manifest YAML returns empty route list (no crash)
- [x] Malformed YAML raises YAML parse error at module load — caught at app startup
- [x] Missing manifest file raises `FileNotFoundError` at module load — caught at app startup
- [ ] YAML anchors referencing non-existent nodes produce cryptic YAML parser errors
- [ ] Duplicate route IDs silently override — last definition wins

## Security

- [x] Manifest API is admin-only (requires operator role)
- [x] Manifest is a server-side file — no client-side patching possible
- [ ] Manifest-based route permission validation not yet implemented (checkbox above)
