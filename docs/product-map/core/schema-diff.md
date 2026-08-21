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
- [x] Best-effort partial-chain fallback — `apply_partial`/`dry_run_partial`/`describe_partial_chain` apply the reachable prefix of a chain and report missing steps instead of raising `MissingMigrationError`

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
- [x] POST /api/v1/schemas/migrate/plan requires an authenticated principal (`schema.list`) — 401/403 for unauthenticated access
- [x] POST /api/v1/schemas/migrate/plan records a `schema_migration_planned` audit event with plan-change counts
- [x] POST /api/v1/schemas/migrate/plan audit failure does not break the response (logged, plan still returned)
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
- [x] Migration gap detection raises error — no degraded fallback for partial migration chains
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
- [x] POST /api/v1/schemas/migrate/plan auth required — `require_permission("schema.list")`, 401/403 without a valid principal
- [x] Schema access is org-scoped — cross-org schema returns 404
- [x] Audit logging for schema migration operations — schema_migration_completed event on POST /api/v1/schemas/migrate (recorded for both apply and dry_run, flagged via `dry_run` payload field)
- [x] Audit logging for migration plan previews — schema_migration_planned event on POST /api/v1/schemas/migrate/plan (plan-change counts in payload)

## Known Gaps

- Circular rename chains (A→B, B→A) — **RESOLVED 2026-08-02**: `apply_migration` now detects rename cycles via `_detect_rename_cycles()` and applies them as value rotations (a full A↔B swap preserves both values) instead of the previous overwrite that lost one field; rename *detection* in `create_migration` is now deterministic (candidate names sorted) so the same schema pair always yields the same plan across runs/hash seeds. Covered by `TestApplyMigration` (swap/3-cycle/chain) + `TestDetectRenameCycles` + determinism tests + BDD scenario (8 scenarios in `schema_migration.feature`).
- ~~POST /api/v1/schemas/migrate/plan is stateless and unaudited by design — it only previews a plan from inline definitions and never touches persisted schemas.~~ **RESOLVED 2026-08-13**: the plan endpoint now records a `schema_migration_planned` audit event (org-scoped, actor + plan-change counts) so plan previews are traceable; audit append failures are logged and never break the response.
- ~~The `/migrate/plan` endpoint has no auth dependency (pure compute on request bodies). Confirm this is acceptable before shipping in a public deployment.~~ **RESOLVED 2026-08-13**: the endpoint now carries `require_permission("schema.list")` — an authenticated tenant principal is required (401/403 without one), consistent with the rest of the schemas API.
- ~~MigrationRegistry's `validate_chain` reports gaps but callers have no degraded fallback — a partial chain is an error, never a best-effort migration.~~ **RESOLVED 2026-08-07**: `MigrationRegistry` now exposes a best-effort partial-chain family — `get_partial_chain()` (returns the reachable prefix + gap descriptions), `apply_partial()` (applies the reachable prefix and reports missing steps instead of raising `MissingMigrationError`), `describe_partial_chain()`, and `dry_run_partial()` (per-step diffs for the applied prefix, never mutating data). A source with no outgoing migration passes data through unchanged. Covered by 14 unit tests (`TestPartialChain` in `test_schema_migration.py`) + 4 BDD scenarios in `schema_migration.feature`.

## QA History

- 2026-08-13 (improve-architecture): Resolved the two `/migrate/plan` gaps in `api/routes/schemas.py`. (1) **Auth dependency** — the endpoint now requires `require_permission("schema.list")`; unauthenticated access returns 401/403 instead of computing a plan anonymously (removed from the ADR 017 route-introspection EXEMPT allowlist — it is now a tagged mutating route). (2) **Audit logging** — the endpoint appends a `schema_migration_planned` audit event (org-scoped, actor_user_id, resource_type `schema`, payload with field_additions/field_removals/type_changes/renames counts); `ProgrammingError` (missing audit table) and generic audit failures are logged and never break the 200 response. Added 4 unit tests (`test_migration_plan_unauthenticated_returns_4xx`, `test_migration_plan_records_audit_event`, `test_migration_plan_audit_failure_does_not_break_response`, plus audit patches on the 2 existing plan tests) + 2 BDD scenarios in `schema_migration.feature` (plan preview records audit event, plan endpoint requires authentication) with 2 new step definitions. Updated `core/audit-trail.md` (2 event types documented). 57/57 focused unit + BDD tests pass, ruff check + format clean, mypy --strict clean. Status: partial (stateless inline-definition plan preview remains compute-only by design).
- 2026-08-07 (improve-architecture): Resolved the "no degraded fallback for partial migration chains" gap in `MigrationRegistry` (`core/schema_registry/migration.py`). Added a best-effort partial-chain API — `get_partial_chain()` (returns `(reachable_chain, gaps)`; `gaps == []` when the full chain exists), `apply_partial()` (applies the reachable prefix and returns `(migrated_data, gaps)`, never raising `MissingMigrationError`; a source with no outgoing migration passes data through unchanged), `describe_partial_chain()`, and `dry_run_partial()` (per-step diffs for the applied prefix, never mutating data). Extracted the shared `_longest_path()` DFS and `_build_step_reports()` helpers reused by `validate_chain`/`dry_run`. Added 14 unit tests (`TestPartialChain` in `test_schema_migration.py`) + 4 BDD scenarios in `schema_migration.feature` (reachable-prefix apply, complete-chain no-gaps, pass-through on no chain, dry-run prefix) with 7 new step definitions. 103/103 core schema-migration unit tests + 11/11 schema-migration BDD scenarios pass (32/32 schemas endpoint tests unchanged), ruff clean, mypy --strict clean. Status: partial (2 gaps remain — stateless/unaudited `/migrate/plan` + its missing auth dependency).
- 2026-08-02 (improve-architecture): Resolved the circular-rename-chain gap — deterministic rename detection + cycle-safe value-rotation application in `apply_migration` (`_apply_renames`/`_detect_rename_cycles`), 11 new core unit tests, 1 new BDD scenario + plural `fields` step definitions. 89/89 core schema-migration unit tests + 8/8 BDD scenarios pass; ruff clean.
