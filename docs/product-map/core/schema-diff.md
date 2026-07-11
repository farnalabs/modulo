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

## Known Gaps

- **No BDD scenarios for migration plan endpoint** — only migrate endpoint has BDD coverage
- **Rename heuristic is type-only** — same-type fields may be falsely paired across unrelated additions/removals (e.g. two string fields added and removed in the same change set)
- **No concurrency tests for MigrationRegistry** — lock tested only in basic coverage
- **No performance tests** for large migration chains
