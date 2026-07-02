---
id: feat-core-migration-cli
prd: 6.2
delivery-tasks: [task-nv9-migration-cli]
code:
  - backend/src/modulo/cli/migrate.py
  - backend/src/modulo/cli/migrate_org.py
bdd: []
depends-on: []
unit-tests:
  - backend/tests/unit/cli/test_migrate.py
status: partial
---

# Migration CLI

Two implementations exist: `modulo-migrate` (click-based, JSONL format with auth) and `modulo` (argparse-based, JSON format, no auth). Both implement `export-org` / `import-org`.

## Behaviours

### modulo-migrate (click-based, JSONL, authenticated)

- [x] `export-org` exports all org data (users, pipelines, runs, audit events, library primitives, connector instances, model backends) as JSONL
- [x] Auth via `--token` flag, `MODULO_ADMIN_TOKEN` env var, or `MODULO_ADMIN_SECRET` env var
- [x] Non-admin JWT rejected with clear error
- [x] User not in target org rejected
- [x] `--pipelines-only` flag restricts export to pipelines
- [x] `--users-only` flag restricts export to users
- [x] Each record has `__table__`, `id`, `data`, `__hash__` fields
- [x] Metadata header line contains version, export timestamp, aggregate SHA-256 hash
- [x] `verify-export` re-computes per-table hashes and compares to stored hash
- [x] Hash mismatch on verify prints per-table hash comparison and exits non-zero
- [x] `import-org` reads JSONL, verifies hash, imports records
- [x] Hash mismatch on import aborts with error
- [x] `--on-conflict skip` leaves existing records untouched (default)
- [x] `--on-conflict overwrite` replaces existing records fully
- [x] `--on-conflict merge` only fills null/empty fields on existing records
- [x] `--pipelines-only` / `--users-only` flags available on import
- [ ] Progress bars via tqdm on all long-running operations
- [x] Summary output: created/skipped/overwritten/errors counts
- [x] Org not found returns error with "not found" message
- [x] UUID columns serialised as strings
- [x] Datetime columns serialised as ISO 8601
- [x] Binary / blob columns serialised as hex
- [x] Output directory created if missing
- [x] Empty database tables handled without crash
- [x] Non-existent input file returns error

### modulo export-org / import-org (argparse-based, JSON, unauthenticated)

- [x] `export-org --org-id --output` exports org data as single JSON file
- [x] Export includes org entity + users, teams, stages, schemas, schema_versions, model_backends, library_primitives, connector_instances, agents, pipelines, runs
- [x] Export includes SHA-256 bundle hash for integrity verification
- [x] Org not found returns error message
- [ ] Existing output file returns error without `--force`
- [x] `import-org --file` reads JSON file and bulk-imports records
- [x] Import skips existing records (idempotent)
- [x] Import creates missing org automatically (upsert)
- [ ] Non-existent input file returns error

### Edge cases

- [x] Export with zero records in a table produces empty array for that table
- [x] Import into org with partially overlapping data skips existing records
- [x] Network/auth failure during modulo-migrate export produces non-zero exit
- [x] Binary/blob data in table columns serialized as hex strings
- [x] Empty output directory creates it before writing

## Known Gaps

- No unit tests for migrate_org.py (argparse-based modulo CLI) — migrate.py (modulo-migrate) has 26 tests
- No BDD feature files for migration/export behaviour
- modulo-migrate requires auth token or admin secret — no interactive login
- modulo (argparse) has no auth — runs with direct DB access
- No import conflict resolution for audit events (append-only constraint)
- No data validation before import — corrupt JSONL/JSON is accepted
