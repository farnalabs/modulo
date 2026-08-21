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
- Backend consumption of manifest for route permission validation — not implemented (see Known Gaps)
- Auto-generation from manifest to product map entries — not implemented (see Known Gaps)

## Error Handling

- [x] `GET /api/v1/manifest` catches `ProgrammingError` → 501 with migration hint
- [x] `GET /api/v1/manifest` catches `SQLAlchemyError` → 503
- [x] `GET /api/v1/manifest` catches `Exception` → 500 with `logger.exception`
- [x] Frontend manifest YAML parse failure caught during build — build fails with `vue-tsc` type errors

## Edge Cases

- [x] Empty manifest YAML returns empty route list (no crash)
- [x] Malformed YAML raises YAML parse error at module load — caught at app startup
- [x] Missing manifest file raises `FileNotFoundError` at module load — caught at app startup
- [x] Duplicate route IDs are now REJECTED at load — `core/manifest.py` uses a duplicate-key-rejecting SafeLoader so a duplicated route/element/sidebar-group key raises `RuntimeError` instead of silently last-wins; verified by `TestDuplicateKeyDetection` in `test_manifest.py`
- YAML anchors referencing non-existent nodes produce cryptic YAML parser errors (see Known Gaps)

## Security

- Manifest API is not admin-only — `GET /api/v1/manifest` has no auth dependency (PRD §8.28.5 lists it as Community-tier diagnostics); the "operator role required" claim was inaccurate (see Known Gaps)
- [x] Manifest is a server-side file — no client-side patching possible
- Manifest-based route permission validation not yet implemented (see Known Gaps)

## Known Gaps

- **Backend route-permission validation from the manifest is not implemented** — the manifest is loaded and served (`GET /api/v1/manifest`) but no backend route authorisation is derived from `required_roles`/`required_permissions`; role/permission checks use the hardcoded ADR 017 registry (`modulo.auth.permissions.PERMISSIONS`).
- **Auto-generation from manifest to product-map entries is not implemented** — `validate-manifest.ps1` validates product-map refs but nothing generates product-map entries from the manifest.
- **Manifest endpoint is unauthenticated** — `manifest_endpoint()` in `api/routes/manifest.py` has no auth dependency; the manifest exposes routes/testids/i18n keys (no secrets), and PRD §8.28.5 tiers it Community, but the earlier "admin-only (operator role)" claim in this entry was inaccurate and has been corrected.
- **Undefined YAML anchors** (`<<: *missing`) raise PyYAML's native cryptic constructor error rather than a guided message.

## QA History

### 2026-08-15 — distribute (partial→covered sweep)

- **Implemented (duplicate-key rejection):** `core/manifest.py` now loads via `_NoDuplicateKeysLoader`, a `yaml.SafeLoader` subclass that rejects literal duplicate mapping keys (before anchor flattening, so legitimate `<<: *anchor` override points still work). A duplicate route/element/sidebar-group key now raises `RuntimeError("Failed to load manifest ... duplicate mapping key ...")` instead of silently last-wins. 3 new tests in `test_manifest.py` (duplicate route key, duplicate sidebar-group key, anchor-override still loads).
- **Corrected Security checkbox:** the "Manifest API is admin-only (requires operator role)" claim was false — the endpoint carries no auth dependency. Marked unchecked with a Known Gap note.
- **Confirmed genuine gaps** (left unchecked): backend route-permission validation from the manifest, auto-generation to product-map entries, and cryptic undefined-anchor errors.

### 2026-08-15 — coverage sweep (partial-small-a)

- Converted the remaining unchecked behaviour checkboxes to Known Gap bullets (all already documented): backend route-permission validation not implemented, auto-generation to product-map entries not implemented, undefined YAML anchors raising cryptic parser errors, and the manifest endpoint being intentionally unauthenticated (Community-tier diagnostics per PRD §8.28.5). No code change. Status: partial (13/18 — all remaining unchecked items are documented gaps).
