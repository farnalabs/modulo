# ADR 008 — Core Shared Manifest: Single Source of Truth for Page Structure

**Date**: 2026-07-03  
**Status**: Active

---

## Context

Remy's UI commands work through runtime discovery (`get_page_interactables()`), but page metadata is scattered across five separate sources: Vue Router route config, sidebar nav config, route `meta` breadcrumbs, `data-testid` in Vue templates, product map entries, and i18n locale files. This duplication causes:

1. **Inconsistency** — a route added in the router but missing from the nav config, or a `data-testid` renamed in a template but not updated in tests.
2. **Blind LLM navigation** — Remy must navigate to a page before discovering its elements, adding latency and failure points.
3. **No integrity validation** — no automated check that a `data-testid` referenced anywhere actually exists in a template, or that every product map entry corresponds to a real route.

## Decision

Create a single `frontend/src/manifest.yaml` that serves as the source of truth for:

- Page route names, paths, `data-testid` values, breadcrumb labels, parent hierarchy
- Interactive elements per page with their `data-testid` selectors
- Sidebar group definitions (order, expansion state, simple mode)
- Feature tier requirements (`required_tier`, `feature_flag`)
- Role/permission requirements (`required_roles`, `required_permissions`)
- Product map references (`product_map: feat-*`)
- i18n key references (`i18n_key`)

Both frontend and backend consume this file:
- **Frontend**: Vite imports YAML at build time. Sidebar nav, breadcrumbs, `<FeatureGate>` props, and route guards read from manifest-derived data.
- **Backend**: Loaded at app startup (copied into Docker image, bind-mounted in local dev). Remy's `get_manifest(path?)` tool returns structured page data directly (no HTTP loopback). Exposed as `GET /api/v1/manifest` for diagnostics only.
- **Validation**: `validate-manifest.ps1` checks integrity at pre-commit and CI time — every route matches a router entry, every static `data-testid` exists in a template, every `product_map` ref resolves, no orphaned elements, no circular parent chains.

## Concrete Design

The full specification, including file format, validator rules, migration strategy, and consumption patterns, is documented at `docs/adr/core-manifest-proposal-v2.md`. Key elements:

```yaml
routes:
  /admin/remy:
    name: admin-remy
    testid: page-remy-config
    breadcrumb: Remy Config
    parent: /admin
    product_map: feat-remy
    i18n_key: nav.remy_config
    sidebar_group: remy
    sidebar_order: 1
    type: form_page
    required_tier: community
    required_roles: [admin]
    required_permissions: null
    feature_flag: null

elements:
  /admin/remy:
    - testid: remy-access-save
      type: button
      label: Save Access List
      dynamic_testid: false
    - testid: remy-skills-add
      type: button
      label: Add Skill

sidebar_groups:
  remy:
    label: Remy
    order: 2
    default_expanded: false
    simple_mode: true
```

## Why Not Continue with the Current Scattered Approach

1. **Five sources of truth cannot be kept in sync by convention alone** — every route change requires updates to router config, nav config, breadcrumb meta, product map, and potentially i18n. The validator catches drift only at CI time; before CI, drift silently accumulates.
2. **`get_page_interactables()` is a fallback, not a plan** — runtime discovery costs an LLM round trip (navigate → discover → decide → act). A manifest lets Remy plan the entire workflow in one LLM turn.
3. **Pre-commit validation of `data-testid` integrity is uniquely valuable** — the validator catches renamed testids before they reach CI, preventing Remy from targeting elements that no longer exist.
4. **The manifest is opt-in** — existing routes without manifest entries continue to work. Only routes explicitly added to the manifest get validation and LLM discoverability. No migration pressure.

## What This Means for Code

| Concern | Approach |
|---|---|
| File location | `frontend/src/manifest.yaml` |
| Format | YAML (supports comments and anchors) |
| Frontend import | Vite build-time `import manifest from '@/manifest.yaml'` |
| Backend load | `datetime`-stamped `load_manifest()` at startup; env var `MANIFEST_PATH` for custom path |
| Docker | `COPY frontend/src/manifest.yaml /app/manifest.yaml`; bind mount for local dev |
| Remy tool | `get_manifest(path?)` → returns route + elements for path, or full manifest |
| Validator | `validate-manifest.ps1` with 7 rules, `-CI` flag for hard-fail |
| Migration | `navigation.ts` imports manifest as data source first; programmatic overrides via `getDynamicItemsForGroup()` |

## When to Revisit

- The manifest exceeds 1,000 lines (too large for its purpose — consider splitting by domain)
- A route requires metadata that cannot be expressed in the current schema
- The `deliver` skill is observed to consistently forget to update the manifest (automation gap)
- Performance of the validator on a full codebase becomes a CI bottleneck

## Related Documents

- PRD §8.28 — Core Shared Manifest feature specification
- ADR 007 — Remy UI Commands: Frontend-Mediated Browser Automation
- `frontend/src/manifest.yaml` — the manifest file (created in phase nv36)
- `Dev-Harness/tools/validate-manifest.ps1` — validation script
