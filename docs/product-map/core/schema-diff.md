---
id: feat-core-schema-diff
prd: 8.3
bdd:
  - backend/tests/bdd/features/schemas/schema_migration.feature
unit-tests:
  - backend/tests/unit/core/test_schema_migration.py
  - backend/tests/unit/api/test_schemas_endpoint.py
code:
  - backend/src/modulo/core/schema_registry/migration.py
  - backend/src/modulo/api/routes/schemas.py
delivery-tasks: []
depends-on: [feat-core-schema-system]
status: partial
---

# Schema Diff / Migration

Compute structural diffs between two JSON Schema definitions and transform data between versions. Used by the migration system to detect field additions, removals, type changes, and renames.

## Behaviours

### Migration Plan

- [x] Compute migration plan between two schema definitions
- [x] Detect field additions with type inference
- [x] Detect field removals
- [x] Detect type changes with old/new type info
- [x] Detect renames via same-type heuristic matching
- [x] Handle empty/missing properties gracefully

### Migration Registry

- [x] Register migration functions between source/target versions
- [x] Resolve multi-step migration chains via BFS
- [x] Detect missing migration gaps via validate_chain
- [x] Apply full migration chain to data (transform in order)
- [x] Dry-run returns per-step field-level diff without mutating data
- [x] Describe chain without running it

### Data Transformation

- [x] Rename fields
- [x] Convert field values via custom converter functions
- [x] Add fields with default values
- [x] Add computed fields via value function
- [x] Remove fields
- [x] Handle rename conflicts (target field already exists) with warning

### API Endpoints

- [x] POST /api/v1/schemas/migrate — migrate data between schemas with dry_run support
- [x] POST /api/v1/schemas/migrate/plan — preview migration plan without applying
- [x] Missing source/target schema returns 404
- [x] Schemas with no versions return 404
- [x] ProgrammingError returns 501
- [x] SQLAlchemyError returns 503
- [x] Exception returns 500 with logging

## Error Handling

- [x] Missing source/target schema returns 404
- [x] Schemas with no versions return 404
- [x] ProgrammingError returns 501
- [x] SQLAlchemyError returns 503
- [x] Exception returns 500 with logging
- [x] Rename conflicts (target field already exists) handled with warning — migration continues
- [x] Empty/missing properties handled gracefully
- [ ] Migration gap detection raises error — no degraded fallback for partial migration chains
- [x] Audit event append failure does not break the migration response (logged, migration still returns 200)

## Edge Cases

- [x] Empty/missing properties in both source and target handled
- [x] Same source and target schema — empty migration plan (no changes detected)
- [x] Single-field schema — diff detected correctly
- [x] Large schemas (1000+ fields) — diff computation and apply scale test
- [x] Circular rename chains (A→B, B→A) — detected via `_detect_rename_cycles` and applied as a value rotation (swap), no data loss
- [x] Non-existent version in migration chain gap detection — MissingMigrationError raised
- [x] Audit table missing (ProgrammingError on append) — logged, migration succeeds

## Security

- [x] Auth required — 401 for unauthenticated access
- [x] Schema access is org-scoped — cross-org schema returns 404
- [x] Audit logging for schema migration operations — schema_migration_completed event on POST /api/v1/schemas/migrate (recorded for both apply and dry_run, flagged via `dry_run` payload field)

## Known Gaps

- Circular rename chains (A→B, B→A) — **RESOLVED 2026-08-02**: `apply_migration` now detects rename cycles via `_detect_rename_cycles()` and applies them as value rotations (a full A↔B swap preserves both values) instead of the previous overwrite that lost one field; rename *detection* in `create_migration` is now deterministic (candidate names sorted) so the same schema pair always yields the same plan across runs/hash seeds. Covered by `TestApplyMigration` (swap/3-cycle/chain) + `TestDetectRenameCycles` + determinism tests + BDD scenario (8 scenarios in `schema_migration.feature`).
- POST /api/v1/schemas/migrate/plan is stateless and unaudited by design — it only previews a plan from inline definitions and never touches persisted schemas.
- The `/migrate/plan` endpoint has no auth dependency (pure compute on request bodies). Confirm this is acceptable before shipping in a public deployment.
- MigrationRegistry's `validate_chain` reports gaps but callers have no degraded fallback — a partial chain is an error, never a best-effort migration.

## QA History

- 2026-08-02 (improve-architecture): Resolved the circular-rename-chain gap — deterministic rename detection + cycle-safe value-rotation application in `apply_migration` (`_apply_renames`/`_detect_rename_cycles`), 11 new core unit tests, 1 new BDD scenario + plural `fields` step definitions. 89/89 core schema-migration unit tests + 8/8 BDD scenarios pass; ruff clean.
