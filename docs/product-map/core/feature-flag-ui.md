---
id: feat-core-feature-flag-ui
prd: 8.17
delivery-tasks: [task-nv12-feature-flag-ui]
code:
  - backend/src/modulo/core/feature_flags.py
  - backend/src/modulo/core/license.py
  - backend/src/modulo/api/routes/admin_feature_flags.py
  - backend/src/modulo/api/routes/admin_license.py
  - backend/src/modulo/api/routes/viewmodel.py
  - backend/src/modulo/db/crud/tier_catalog.py
  - frontend/src/views/AdminFeatureFlagsView.vue
  - frontend/src/views/SettingsLicenseView.vue
  - frontend/src/components/FeatureGate.vue
  - frontend/src/components/LockIcon.vue
  - frontend/src/components/SidebarFooter.vue
  - frontend/src/stores/planStore.ts
  - frontend/src/router/index.ts

bdd: []
depends-on: [feat-core-db-abstraction-core]
unit-tests:
  - backend/tests/unit/api/test_admin_feature_flags.py
  - backend/tests/unit/core/test_feature_flag_registration.py
  - backend/tests/unit/api/test_admin_license.py
  - frontend/src/__tests__/AdminFeatureFlagsView.spec.ts
status: partial
---

# Feature Flag UI

Feature flag inspection dashboard at `/admin/feature-flags` listing all known flags, their tier, and current activation status. Also manages Tier badge in sidebar nav footer, Lock icon on gated features, and License settings page at `/settings/license`.

## Behaviours

### Backend — Feature Flag Registry

- [x] `FeatureFlagRegistry` catalogs all known flags with name, description, tier, and active status
- [x] Seven Community-tier flags registered: `parallel_branches`, `eval_system`, `webhook_trigger`, `cron_trigger`, `mcp_server`, `community_library`, `saved_views`
- [x] Additional Community-tier flags: `polling_trigger`, `agent_signal_trigger`, `helm_deployment`, `model_backend_management`, `remy`
- [x] Nine Team-tier flags registered: `sso`, `team_rbac`, `audit_viewer`, `admin_spend_limits`, `observability`, `view_modes`
- [x] Additional Team-tier flags: `admin_cost_controls`, `admin_cost_breakdown`, `admin_run_retention`, `error_forwarders`, `schema_version_history`, `environment_profiles`, `plugin_management`
- [x] Two v1-tier flags registered: `schema_union_types`, `migration_cli`
- [x] Five v2-tier flags registered: `checkpoint_encryption`, `audit_crypto_chain`, `community_registry`, `prompt_optimization`, `pipeline_diff_rollback`
- [x] Active status determined by license state: tier rank order (community → team → v1 → v2), all flags at or below current tier are active
- [x] `tier_gap_flags()` returns flags above community tier that are currently inactive due to Community tier
- [x] `get_flag(name)` returns flag by name or None
- [x] `list_flags()` returns list of all flags
- [x] `set_override(name, enabled)` / `clear_override(name)` / `get_override(name)` manage in-memory flag overrides (class-level, shared across requests)
- [x] `from_db(session)` loads flags from DB tier/feature catalog (replaces hardcoded data)
- [x] `load_from_db(session)` replaces hardcoded data with DB-backed tier and flag definitions
- [x] `refresh(tier, has_key)` re-evaluates active status after a tier change

### Backend — Plan Context Resolution

- [x] `resolve_plan_context()` resolves plan in 4-level priority: org-level license → in-memory license → env-var license → community fallback
- [x] `CommunityTier` class provides plan context with all community features active, no license key
- [x] `LicenseKeyTier` class provides plan context with explicit feature list override
- [x] `DbPlanContext` class resolves from DB tier catalog
- [x] `get_plan_for_org()` queries organisation plan or system default

### Backend — API `GET /api/v1/admin/feature-flags`

- [x] Returns `license` object: `tier`, `has_license_key`, `is_valid`
- [x] Returns `flags` array with each flag's `name`, `description`, `tier`, `currently_active`, `depends_on`
- [x] Returns `would_activate` array of flags that would become active with a license key
- [x] License tier inferred from `MODULO_LICENSE_KEY` presence: present → `"team"` (or parsed key tier), absent → `"community"`
- [x] `is_valid` is always `True` (no actual cryptographic license verification in this endpoint — `parse_and_verify` is called in `_build_registry` for license key parsing)
- [x] Returns 501 Not Implemented on `ProgrammingError` (DB migration needed)
- [x] Returns 500 with `INTERNAL_ERROR` code on unexpected failures

### Backend — API `GET /api/v1/admin/feature-flags/{flag_name}`

- [x] Returns single flag with `name`, `description`, `tier`, `currently_active`, `depends_on`
- [x] Unknown flag name returns 404 with detail message
- [x] Returns 501 Not Implemented on `ProgrammingError`
- [x] Returns 500 with `INTERNAL_ERROR` code on unexpected failures

### Backend — API `PUT /api/v1/admin/feature-flags/{flag_name}` (toggle override)

- [x] Toggles flag override via `set_override(name, enabled)`
- [x] Returns updated flag shape with `overridden: true`
- [x] Unknown flag name returns 404 before mutating
- [x] Returns 501 Not Implemented on `ProgrammingError`
- [x] Returns 500 with `INTERNAL_ERROR` code on unexpected failures

### Backend — API `GET /api/v1/license` (public)

- [x] Returns `tier` (`"community"` or `"team"`), `features` list, `is_valid` boolean
- [x] Features list always includes `["notifications"]` when license key present
- [x] Returns `{"tier": "community", "features": [], "is_valid": true}` when no key present
- [x] Hardcodes `is_valid: True` — no expired/invalid key differentiation (uses `bool(settings.modulo_license_key)` only)

### Backend — API `POST /api/v1/admin/license` / `GET /api/v1/admin/license`

- [x] POST accepts signed license key, parses and verifies via `parse_and_verify()`
- [x] Rejects tampered payloads with 400
- [x] Rejects expired licenses with 400 and specific message
- [x] Rejects malformed base64 with 400
- [x] Stores validated license in-memory via `store_license()`
- [x] GET returns current license status from in-memory store
- [x] `clear_license()` on GET when license expired

### Frontend — AdminFeatureFlagsView.vue

- [x] Route at `/admin/feature-flags` backed by `AdminFeatureFlagsView`
- [x] License Status card shows Tier label, License Key badge (Active / Not set), Status badge (Valid / Invalid), Expires date
- [x] "Would activate with a license key" section lists team/v1/v2 flags with tier label
- [x] Flags grouped by tier section headers (Community, Team, v1, v2)
- [x] Search/filter input filters flags by name or description
- [x] Pagination controls (Previous/Next, page count, page size=10)
- [x] Loading spinner while fetching
- [x] Error state with message and Retry button
- [x] Fetches on mount via `loadFlags()`
- [x] Toggle switches per flag row (calls PUT toggle endpoint)
- [x] Plan info header: planStore.currentTier, enabled/total count, Team badge
- [x] Search resets to page 1 on query change
- [x] No filter implementation — all flags always shown (search trims results)

### Frontend — Tier badge in sidebar (PRD Tier badge)

- [x] Tier badge pill in sidebar nav footer reading from planStore
- [x] Community tier shows `Community` badge (neutral colour)
- [x] Team tier shows `Team` badge (accent colour) with expiry tooltip
- [x] Badge links to `/settings/license`

### Frontend — Lock icon on gated features (PRD Team feature gate)

- [x] FeatureGate component with modes: `show-disabled` (40% opacity) or full lock wall
- [x] LockIcon component for gated features
- [ ] Tooltip says "Requires a Team license — see /settings/license" (actual text: "Available on a higher plan tier" and links to modulo.run/pricing)
- [ ] Lock icon links to `/settings/license` (actual link: modulo.run/pricing)

### Frontend — License settings page (`SettingsLicenseView.vue`)

- [x] Route at `/settings/license` (admin only)
- [x] Current tier card: `Community` or `Team` with expiry date and licensed org name
- [x] Active features checklist: each feature flag shows enabled (✓) or disabled (✗ with "requires Team")
- [x] License key management: textarea to paste new key
- [x] "Verify key" dry-run button before applying
- [x] Confirmation dialog on apply with server restart warning
- [x] Upgrade CTA shown on Community tier
- [x] Apply / Remove license buttons with confirmation dialogs

### Frontend — planStore.ts (Pinia store)

- [x] Fetches plan from `/api/v1/admin/license` and feature flags from `/api/v1/admin/feature-flags`
- [x] Computed `isTeam`, `currentTier`, `expiresAt`, `features`, `isLoading`
- [x] `fetchPlan()` called on init
- [x] `getTierLabel(tier)` returns human-readable tier label

### Error Handling

- [x] GET list route: returns 501 on `ProgrammingError` with migration hint
- [x] GET by-name route: returns 501 on `ProgrammingError` with migration hint
- [x] PUT toggle route: returns 501 on `ProgrammingError` with migration hint
- [x] All 3 routes catch unexpected Exception and return 500 with `INTERNAL_ERROR` code
- [x] Unknown flag name returns 404 with specific flag name in detail
- [x] Frontend catches network errors and displays user-facing error message with Retry
- [x] Frontend handles undefined/null API fields gracefully (null-coalescing fallbacks)
- [x] Frontend search empty state shows `"No feature flags match your search."`
- [x] Frontend planStore.fetchPlan() uses Promise.allSettled — one failing API doesn't cascade to block other data
- [x] Frontend planStore.fetchPlan() has 15s timeout for each API call via Promise.race
- [ ] Frontend planStore has no retry mechanism on failure — error is recorded but not retried
- [ ] `GET /api/v1/admin/license` (admin_license.py) has no ProgrammingError handling — missing DB tables propagate as 500
- [ ] `GET /api/v1/license` (viewmodel.py) has no error handling at all — no try/except wrapper
- [ ] `_build_registry` `has_key` does not check org-level license keys — org-level keys are ignored in the `has_license_key` response field

### Resilience

- [x] Frontend error state shows Retry button calling `loadFlags()` again
- [x] Frontend `SettingsLicenseView.vue` shows ErrorAlert with retry for load failures
- [x] AdminFeatureFlagsView uses `??` fallbacks for nullable API fields
- [ ] No degraded-mode fallback to hardcoded `_KNOWN_FLAGS` when DB is unreachable — `FeatureFlagRegistry.from_db` requires DB
- [ ] `FeatureFlagRegistry._overrides` is a `ClassVar[dict]` shared across requests — not thread-safe in async context
- [ ] Flag overrides lost on server restart — in-memory only, no persistence layer
- [ ] `load_from_db()` partial-failure inconsistency — `_tier_rank` may update before `_flags` if the second DB call fails
- [ ] GET /api/v1/admin/license (admin_license.py) rolls back an opened transaction on ProgrammingError without consuming the error cleanly — the exception propagates through the session context manager, which may leave the session in a closed state

### Edge Cases

- [x] License key absent → all team/v1/v2 flags show Inactive
- [x] License key present → all flags at or below team tier show Active
- [x] Unknown flag name via API → 404 returned
- [x] Network error caught and displayed as user-facing message
- [x] Invalid/undefined API response → fallback defaults prevent render crash
- [x] Search with no results shows empty state message
- [x] Empty flags list at a tier shows "No flags in this tier."
- [ ] DB unreachable (connection error, timeout) — returns 500 with no degraded-mode fallback to hardcoded flags
- [ ] Flag overrides (`_overrides` ClassVar) are not thread-safe in async context
- [ ] Flag overrides lost on server restart (in-memory only, no persistence)
- [ ] `GET /api/v1/license` always returns `is_valid: true` — does not validate the license key signature or expiry
- [ ] `load_from_db()` partial-failure inconsistency — `_tier_rank` may update before `_flags` if the second DB call fails

### Test Coverage

- [x] 19 backend API tests exist for admin feature-flags routes (3 routes + ProgrammingError + catch-all)
- [x] 3 backend unit tests for flag registration (saved_views only)
- [x] 18 backend tests for admin license management (parse/verify, GET/POST)
- [x] 11 frontend component tests for AdminFeatureFlagsView
- [x] All 19 API tests pass in isolated unit-test environment (no Postgres needed)
- [ ] No FeatureFlagRegistry core unit tests (list_flags, get_flag, tier_gap_flags, refresh, overrides)
- [ ] No tests for PlanContext classes (CommunityTier, LicenseKeyTier, DbPlanContext, resolve_plan_context)
- [ ] No tests for GET /api/v1/license (viewmodel.py) endpoint
- [ ] No BDD feature files for feature flag inspection
- [ ] No frontend tests for error/loading/empty states in AdminFeatureFlagsView
- [ ] No tests for SettingsLicenseView or planStore

## QA History (index 136 — cross-cutting)

### Findings fixed

- **CRITICAL:** `test_admin_feature_flags.py` had no `get_db_session` mock — all happy-path tests would fail without a running Postgres instance. Added `get_db_session` and `_get_engine` dependency overrides + `_mock_registry()` helper to provide a DB-free `FeatureFlagRegistry`.
- **CRITICAL:** Missing PUT toggle tests — entire `TestToggleFeatureFlag` class absent. Added 4 tests (happy 200, unknown 404, unauth 401/403, error 500).
- **CRITICAL:** Missing ProgrammingError→501 tests for all 3 routes. Added `TestProgrammingError` class (3 tests covering GET list, GET by-name, PUT toggle).
- **CRITICAL:** `test_admin_feature_flags.py:TestGetFeatureFlag.test_returns_200_for_known_flag` would fail with 500 (no DB). Fixed with `_build_registry` patching.
- **CRITICAL:** `TestCatchAllMiddlewareFallback.test_plain_json_on_serialization_failure` asserted wrong response structure (expected `error.code` but middleware returns flat `type/title/detail/status`). Fixed assertion.
- **MAJOR:** Frontend `AdminFeatureFlagsView.vue` used `\`Failed to load feature flags: ${err}\`` template literal interpolation — produces `[object Object]` for error objects. Fixed: imported `formatApiError` and wrapped all error template literals.
- **MAJOR:** Product map frontmatter `unit-tests: []` was wrong — 4 test files exist (19 API tests + 3 core tests + 18 license tests + 11 frontend tests). Updated `unit-tests` field.
- **MAJOR:** Product map frontmatter `code:` was missing 7+ file paths (SettingsLicenseView.vue, SidebarFooter.vue, LockIcon.vue, FeatureGate.vue, planStore.ts, admin_license.py, license.py, tier_catalog.py). Updated.
- **MAJOR:** All 47+ behaviour checkboxes were marked `[ ]` but most are implemented. Marked verified `[ ]→[x]` with code evidence.
- **MAJOR:** PUT toggle endpoint + search/filter + pagination + PlanContext protocol + planStore + `load_from_db()` were entirely missing from behaviours. Added.
- **MAJOR:** Tier assignments in product map were stale (code has different tiers). Documented actual tiers from `_KNOWN_FLAGS`.

### Unresolved findings (carried forward)
- **is_valid always True:** Both `GET /api/v1/license` (viewmodel.py) and the license block in the feature-flags list endpoint always report `is_valid: true` even for expired/tampered/malformed keys. The admin license management endpoint correctly validates, but the public endpoints trust string presence only.
- **Thread-unsafe overrides:** `FeatureFlagRegistry._overrides` is a `ClassVar[dict]` — shared across async requests with no lock. Race conditions possible under concurrent toggle operations.
- **No degraded DB fallback:** `_build_registry` always calls `FeatureFlagRegistry.from_db(session)`. If the DB is unreachable (connection refused, timeout), `from_db` raises an error caught by `except Exception` → returns 500. The hardcoded `_KNOWN_FLAGS` / `TIER_RANK` could serve as a degraded fallback but are never used.
- **Frontend Lock icon links to modulo.run/pricing instead of /settings/license:** `FeatureGate.vue` and `LockIcon.vue` link to the external pricing page rather than the internal license settings page per PRD.
- **Tier badge no "License expired" state:** `SidebarFooter.vue` only distinguishes Community vs Team — expired state shows as `team` badge.
- **No frontend route guard for admin-only routes:** Any authenticated user can reach `/admin/feature-flags` (though RLS prevents viewing data outside their org).
- **No BDD feature files:** No `.feature` files exist for feature flag inspection.

## Known Gaps

### License verification
- `is_valid` is always `True` in `admin_feature_flags.py` (list endpoint) and `viewmodel.py` (GET /api/v1/license) — no cryptographic license verification. The `_build_registry` function checks string presence only, not signature validity. The admin license management endpoint (`admin_license.py`) correctly validates via `parse_and_verify()`.
- `GET /api/v1/license` (viewmodel.py) has zero try/except — settings access is trusted but unguarded.

### Thread safety & persistence
- Flag overrides (`_overrides` ClassVar) are not thread-safe in async context — race conditions on concurrent toggle operations.
- Flag overrides are in-memory only — lost on server restart. No persistence layer.

### DB resilience
- No degraded-mode fallback when DB is unreachable — returns 500 despite hardcoded `_KNOWN_FLAGS` available.
- `load_from_db()` has partial-failure inconsistency — `_tier_rank` updates before `_flags`; if the second DB call fails, registry uses DB ranks against hardcoded flags, producing potentially wrong results.

### Frontend
- No frontend route guard for admin-only routes (any authenticated user can reach `/admin/feature-flags`)
- Lock icon tooltip text and link point to modulo.run/pricing instead of /settings/license per PRD
- No "License expired" badge state in sidebar footer
- Frontend component tests (AdminFeatureFlagsView.spec.ts) do not cover error/loading/empty states
- No frontend tests for SettingsLicenseView or planStore

### Test coverage
- No FeatureFlagRegistry core unit tests (list_flags, get_flag, tier_gap_flags, refresh, overrides, from_db)
- No PlanContext class unit tests (CommunityTier, LicenseKeyTier, DbPlanContext, resolve_plan_context)
- No tests for GET /api/v1/license in viewmodel.py
- No BDD feature files for feature flag inspection or license management
- No SettingsLicenseView or planStore frontend tests

### Tier catalog
- Tier names and feature-to-tier assignments are hardcoded in source; no DB-backed tier catalog mechanism to add/rename tiers without a code deploy (tracked as phase-tier-catalog refactor — PRD §6.2.1)
- Product map tier assignments are stale (code has refined tiers since entry was written)
