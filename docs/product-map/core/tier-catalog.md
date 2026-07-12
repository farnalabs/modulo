---
id: feat-core-tier-catalog
prd: 6
delivery-tasks: []
bdd: []
unit-tests:
  - backend/tests/unit/core/test_feature_flag_registry.py
  - backend/tests/unit/core/test_plan_context.py
code:
  - backend/src/modulo/api/routes/admin_tiers.py
  - backend/src/modulo/db/models/tier_catalog.py
  - backend/src/modulo/db/crud/tier_catalog.py
  - backend/src/modulo/core/feature_flags.py
  - backend/scripts/seed_tier_catalog.py
depends-on:
  - feat-core-feature-flags
status: partial
---

# Tier Catalog

Plan tier definitions and feature flag catalog governing which features are available at each tier.

## Behaviours

### Models

- [x] `TierCatalog` — tier_id, label, rank, requires_license, description
- [x] `FeatureFlagCatalog` — name, description, tier_id, depends_on, is_active

### CRUD

- [x] `list_tiers` — returns all tiers ordered by rank
- [x] `get_tier` — returns a single tier by tier_id
- [x] `list_feature_flags` — returns all active flags ordered by name
- [x] `get_feature_flag` — returns a single flag by name
- [x] `list_feature_flags_by_tier` — returns flags filtered by tier_id

### API

- [x] `GET /api/v1/admin/tiers` returns all plan tiers with labels
- [x] Auth guard via `get_current_user` dependency
- [x] Error handling: IntegrityError→409, ProgrammingError→501, SQLAlchemyError→503, Exception→500 with `logger.exception`
- [x] `asyncio.CancelledError` guard before generic exception handlers

### Feature Flag Registry

- [x] `FeatureFlagRegistry` loads from DB-backed tier_catalog with `_KNOWN_FLAGS` fallback
- [x] `DbPlanContext` resolves feature flags from DB tiers
- [x] `resolve_plan_context` resolves org-level → system-level → community tier fallback
- [x] Per-flag overrides at user, team, and org level via `resolve_flag`

### Seed Script

- [x] `seed_tier_catalog.py` seeds Community (rank 0) and Team (rank 1) tiers
- [x] Seeds all feature flags assigned to known tiers

## QA History

### 2026-07-12 — Round 3 QA

- **Fixed (MAJOR):** Removed duplicate `admin_cost_controls` entry in `seed_tier_catalog.py` (appeared twice — same tier_id, description, and name). The duplicate would cause `ON CONFLICT DO NOTHING` to silently skip it on insert, so it was harmless at runtime but misleading.
- **Fixed (MINOR):** No stale frontmatter found. All `code:`, `bdd:`, `unit-tests:` entries verified as accurate.

## Known Gaps

- No dedicated unit tests for `admin_tiers` route or `tier_catalog` CRUD. Tier catalog functions are tested indirectly through `test_feature_flag_registry.py` and `test_plan_context.py` (which mock `tier_catalog` functions).
- No BDD feature files for tier catalog operations.
- `seed_tier_catalog.py` is out of sync with `_KNOWN_FLAGS` in `feature_flags.py`:
  - `observability` is `community` in `_KNOWN_FLAGS` but `team` in seed
  - `remy_ui_driving`, `notification_log`, `api_changelog`, `email_config`, `error_tracking`, `scim`, `external_secrets`, `schema_union_types`, `migration_cli`, `checkpoint_encryption`, `audit_crypto_chain`, `community_registry`, `prompt_optimization`, `pipeline_diff_rollback`, `pipeline_delete`, `rate_limits`, `runtime_config` are in `_KNOWN_FLAGS` but missing from the seed
- No CRUD endpoints for individual tier/flag management (create, update, delete) — only read/list endpoints exist.
- No frontend integration consuming the tiers endpoint (frontend still hardcodes tier labels per PRD §6.2 migration path item 5).
