---
id: feat-core-feature-flag-ui
prd: 8.17, Enterprise tier, License model
delivery-tasks: [task-nv12-feature-flag-ui]
bdd:
code:
  - backend/src/modulo/core/feature_flags.py
  - backend/src/modulo/api/routes/admin_feature_flags.py
  - backend/src/modulo/api/routes/viewmodel.py (GET /api/v1/license)
  - frontend/src/views/AdminFeatureFlagsView.vue
  - frontend/src/router/index.ts (/admin/feature-flags route)
depends-on: []
status: partial
---

# Feature Flag UI

Feature flag inspection dashboard at `/admin/feature-flags` listing all known flags, their tier, and current activation status. Enterprise tier gating uses a cryptographic license key (`MODULO_LICENSE_KEY`). The PRD also specifies a `/settings/license` management page and sidebar tier badge that are not yet implemented.

## Behaviours

### Backend — Feature Flag Registry
- [ ] `FeatureFlagRegistry` catalogs all known flags with name, description, tier, and active status
- [ ] Six free-tier flags registered: `parallel_branches`, `eval_system`, `webhook_trigger`, `cron_trigger`, `mcp_server`, `community_library`
- [ ] Four enterprise-tier flags registered: `sso`, `team_rbac`, `audit_viewer`, `admin_spend_limits`
- [ ] Six v1-tier flags registered: `polling_trigger`, `agent_signal_trigger`, `schema_union_types`, `migration_cli`, `helm_deployment`
- [ ] Five v2-tier flags registered: `checkpoint_encryption`, `audit_crypto_chain`, `community_registry`, `prompt_optimization`, `pipeline_diff_rollback`
- [ ] Active status determined by license state: tier rank order (free → enterprise → v1 → v2), all flags at or below current tier are active
- [ ] `tier_gap_flags()` returns enterprise/v1/v2 flags that are currently inactive due to free tier

### Backend — API `GET /api/v1/admin/feature-flags`
- [ ] Returns `license` object: `tier`, `has_license_key`, `is_valid`
- [ ] Returns `flags` array with each flag's `name`, `description`, `tier`, `currently_active`, `depends_on`
- [ ] Returns `would_activate` array of flags that would become active with a license key
- [ ] License tier inferred from `MODULO_LICENSE_KEY` presence: present → `"enterprise"`, absent → `"free"`
- [ ] `is_valid` is always `True` (no actual signature verification implemented yet — known gap)

### Backend — API `GET /api/v1/admin/feature-flags/{flag_name}`
- [ ] Returns single flag with `name`, `description`, `tier`, `currently_active`, `depends_on`
- [ ] Unknown flag name returns 404 with detail message

### Backend — API `GET /api/v1/license`
- [ ] Returns `tier` (`"free"` or `"enterprise"`), `features` list, `is_valid` boolean
- [ ] Features list always includes `["notifications"]` when license key present
- [ ] Returns `{"tier": "free", "features": [], "is_valid": true}` when no key present
- [ ] Hardcodes `is_valid: True` — no expired/invalid key differentiation yet

### Frontend — AdminFeatureFlagsView.vue
- [ ] Route at `/admin/feature-flags` backed by `AdminFeatureFlagsView`
- [ ] License Status card shows Tier label, License Key badge (Active / Not set), Status badge (Valid / Invalid)
- [ ] "Would activate with a license key" section lists enterprise/v1/v2 flags with tier label
- [ ] Table of all flags with columns: Flag (mono font), Tier (coloured badge), Status (Active / Inactive with dot), Description
- [ ] Tier badge colours: free=green, enterprise=purple, v1=blue, v2=indigo, fallback=gray
- [ ] Active badge green, inactive badge gray with status dot
- [ ] Loading spinner while fetching
- [ ] Error state with message and Retry button
- [ ] Fetches on mount via `loadFlags()`

### Frontend — Tier badge in sidebar (PRD Tier badge)
- [ ] Tier badge pill in sidebar nav footer reading from planStore
- [ ] Free tier shows `Free` badge (neutral colour)
- [ ] Enterprise tier shows `Enterprise` badge (accent colour) with expiry tooltip
- [ ] License expired shows `License expired` badge (destructive colour)
- [ ] Badge links to `/settings/license`
- [ ] Not implemented yet — known gap

### Frontend — Lock icon on gated features (PRD Enterprise feature gate)
- [ ] Enterprise-gated features show lock icon + disabled control instead of being hidden
- [ ] On click/focus, tooltip: "Requires an Enterprise license — see /settings/license"
- [ ] Lock icon links to `/settings/license`
- [ ] Not implemented yet — known gap

### Frontend — License settings page (PRD License page)
- [ ] Route at `/settings/license` (admin only)
- [ ] Current tier card: `Free Tier` or `Enterprise` with expiry date and licensed org name
- [ ] Active features checklist: each feature flag shows enabled (✓) or disabled (✗ with "requires Enterprise")
- [ ] License key management: textarea to paste new key
- [ ] "Verify key" dry-run button before applying
- [ ] Confirmation dialog on apply with server restart warning
- [ ] Upgrade CTA shown on free tier
- [ ] Not implemented yet — known gap

### States
- [ ] Loading spinner while fetching flags
- [ ] Error state with message and Retry button
- [ ] Empty flags list renders empty table body
- [ ] No filter implementation — all flags always shown

### Edge Cases
- [ ] License key absent → all enterprise/v1/v2 flags show Inactive
- [ ] License key present → all flags at or below enterprise tier show Active
- [ ] Unknown flag name via API → 404 returned
- [ ] Network error caught and displayed as user-facing message
- [ ] Invalid server response → `undefined` guard prevents render crash

### Unit / Integration Tests
- [ ] No backend unit tests for `FeatureFlagRegistry`
- [ ] No backend tests for admin feature-flags API routes
- [ ] No frontend component tests for `AdminFeatureFlagsView`
- [ ] No BDD feature files for feature flag inspection

## Known Gaps
- `is_valid` is always `True` in both `admin_feature_flags.py` and `viewmodel.py` — no actual cryptographic license verification or expired-key differentiation is implemented. The `_build_registry` function in `admin_feature_flags.py` only checks string presence, not signature validity.
- No frontend route guard for admin-only routes (any authenticated user can reach `/admin/feature-flags`)
- No `planStore` Pinia store exists yet — the PRD specifies one hydrated from `GET /api/v1/license` on page load
- No sidebar tier badge exists in any nav component
- No lock icon / gating visible on any enterprise-gated feature in the frontend
- No `/settings/license` page exists (not in the router, no view component)
- No unit tests, integration tests, or BDD feature files for any of the feature-flag UI surface
- The `LicenseInfo` model in `viewmodel.py` omits `org_name` and `expires_at` fields specified in the PRD; also hardcodes features to `["notifications"]` instead of deriving from the license payload
- License key management (paste, verify, apply, confirmation dialog, server restart warning) is entirely unimplemented
