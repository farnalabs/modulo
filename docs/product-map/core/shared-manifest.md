---
id: feat-core-shared-manifest
prd: 8.28
delivery-tasks: []
bdd: []
unit-tests: []
code:
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
