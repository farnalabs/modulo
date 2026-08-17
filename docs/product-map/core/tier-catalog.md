---
id: feat-core-tier-catalog
prd: 6
delivery-tasks: []
bdd: []
unit-tests:
  - backend/tests/unit/core/test_feature_flag_registry.py
  - backend/tests/unit/core/test_plan_context.py
  - backend/tests/unit/mcp/test_mcp_config_tools.py
code:
  - backend/src/modulo/api/routes/admin_tiers.py
  - backend/src/modulo/db/models/tier_catalog.py
  - backend/src/modulo/db/crud/tier_catalog.py
  - backend/src/modulo/core/feature_flags.py
  - backend/scripts/seed_tier_catalog.py
depends-on:
  - feat-core-feature-flag-ui
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
- [x] `list_feature_flags` — returns all active flags ordered by name
- [x] `get_feature_flag` — returns a single flag by name

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

## Error Handling

- [x] IntegrityError → 409 Conflict
- [x] ProgrammingError → 501 Not Implemented
- [x] SQLAlchemyError → 503 Service Unavailable
- [x] Exception → 500 with `logger.exception`
- [x] `asyncio.CancelledError` guard before generic exception handlers
- [x] Seed script validates its output — reads back the seeded tier/flag row counts and fails loudly (`RuntimeError`) if either table is empty, instead of unconditionally printing "seeded successfully"

## Edge Cases

- [x] Empty tier catalog returns empty list
- [x] Unknown tier_id returns 404
- [x] Unknown flag name returns 404
- [x] Seed script idempotency — duplicate seed runs are no-ops via `ON CONFLICT (tier_id) DO NOTHING` / `ON CONFLICT (name) DO NOTHING`

## Security

- [x] Auth guard via `get_current_user` dependency
- [x] All tier/flag routes require admin role

## Known Gaps

- Tier rank conflict — two tiers with the same `rank` produce undefined ordering (no uniqueness constraint on `rank`)
- Feature flag `depends_on` circular reference — no cycle detection (a flag depending on itself/its own dependency chain is not rejected)
- No audit logging for tier catalog reads — the admin read routes emit no audit events

## QA History

- 2026-08-15: feat-core-tier-catalog → partial, product-map coverage sweep: **RESOLVED the standalone `seed_tier_catalog.py` drift** — it now imports `TIERS`/`FLAGS` from `modulo.core.seed_data.catalog` (the same source the boot-time `_seed_tier_catalog` uses) instead of maintaining a stale private copy, so `observability` is `community` again and the previously-missing flags (`notification_log`, `api_changelog`, `email_config`, `error_tracking`, `scim`, `external_secrets`, `schema_union_types`, `migration_cli`, `checkpoint_encryption`, `audit_crypto_chain`, `community_registry`, `prompt_optimization`, `pipeline_diff_rollback`, `pipeline_delete`, `rate_limits`, `runtime_config`, etc.) can never drift again. Added output validation: the script reads back the seeded row counts and raises on an empty table. Marked the "Seed script idempotency" and "Seed script output validation" checkboxes `[ ]`→`[x]` (idempotency via `ON CONFLICT ... DO NOTHING` was already implemented; validation added this session). Remaining genuine gaps kept unchecked: tier-rank conflict ordering, `depends_on` cycle detection, and audit logging for tier-catalog reads (none PRD-mandated).
- No dedicated unit tests for `admin_tiers` route or `tier_catalog` CRUD. Tier catalog functions are tested indirectly through `test_feature_flag_registry.py` and `test_plan_context.py` (which mock `tier_catalog` functions).
- No BDD feature files for tier catalog operations.
- No CRUD endpoints for individual tier/flag management (create, update, delete) — only read/list endpoints exist.
- No frontend integration consuming the tiers endpoint (frontend still hardcodes tier labels per PRD §6.2 migration path item 5).
