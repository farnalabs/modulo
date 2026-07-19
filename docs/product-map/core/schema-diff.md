---
id: feat-core-schema-diff
prd: 8.3
bdd:
  - backend/tests/bdd/features/schemas/schema_migration.feature
unit-tests:
  - backend/tests/unit/core/test_schema_migration.py
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

## Edge Cases

- [x] Empty/missing properties in both source and target handled
- [x] Same source and target schema — empty migration plan (no changes detected)
- [x] Single-field schema — diff detected correctly
- [ ] Large schemas (1000+ fields) — no performance testing for diff computation
- [ ] Circular rename chains (A→B, B→A) — may produce unpredictable results
- [ ] Non-existent version in migration chain gap detection

## Security

- [x] Auth required — 401 for unauthenticated access
- [x] Schema access is org-scoped — cross-org schema returns 404
- [ ] No audit logging for schema migration operations

## Known Gaps
