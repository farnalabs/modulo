---
id: feat-core-migration-cli
prd: 6.2
delivery-tasks: [task-nv9-migration-cli]
code:
  - backend/src/modulo/cli/migrate.py
  - backend/src/modulo/cli/migrate_org.py
status: partial
---

# Migration CLI

Two implementations exist: `modulo-migrate` (click-based, JSONL format with auth) and `modulo` (argparse-based, JSON format, no auth). Both implement `export-org` / `import-org`.

## Behaviours

### modulo-migrate (click-based, JSONL, authenticated)

- [ ] `export-org` exports all org data (users, pipelines, runs, audit events, library primitives, connector instances, model backends) as JSONL
- [ ] Auth via `--token` flag, `MODULO_ADMIN_TOKEN` env var, or `MODULO_ADMIN_SECRET` env var
- [ ] Non-admin JWT rejected with clear error
- [ ] User not in target org rejected
- [ ] `--pipelines-only` flag restricts export to pipelines
- [ ] `--users-only` flag restricts export to users
- [ ] Each record has `__table__`, `id`, `data`, `__hash__` fields
- [ ] Metadata header line contains version, export timestamp, aggregate SHA-256 hash
- [ ] `verify-export` re-computes per-table hashes and compares to stored hash
- [ ] Hash mismatch on verify prints per-table hash comparison and exits non-zero
- [ ] `import-org` reads JSONL, verifies hash, imports records
- [ ] Hash mismatch on import aborts with error
- [ ] `--on-conflict skip` leaves existing records untouched (default)
- [ ] `--on-conflict overwrite` replaces existing records fully
- [ ] `--on-conflict merge` only fills null/empty fields on existing records
- [ ] `--pipelines-only` / `--users-only` flags available on import
- [ ] Progress bars via tqdm on all long-running operations
- [ ] Summary output: created/skipped/overwritten/errors counts
- [ ] Org not found returns error with "not found" message
- [ ] UUID columns serialised as strings
- [ ] Datetime columns serialised as ISO 8601
- [ ] Binary / blob columns serialised as hex
- [ ] Output directory created if missing
- [ ] Empty database tables handled without crash
- [ ] Non-existent input file returns error

### modulo export-org / import-org (argparse-based, JSON, unauthenticated)

- [ ] `export-org --org-id --output` exports org data as single JSON file
- [ ] Export includes org entity + users, teams, stages, schemas, schema_versions, model_backends, library_primitives, connector_instances, agents, pipelines, runs
- [ ] Export includes SHA-256 bundle hash for integrity verification
- [ ] Org not found returns error message
- [ ] Existing output file returns error without `--force`
- [ ] `import-org --file` reads JSON file and bulk-imports records
- [ ] Import skips existing records (idempotent)
- [ ] Import creates missing org automatically (upsert)
- [ ] Non-existent input file returns error

### Edge cases

- [ ] Export with zero records in a table produces empty array for that table
- [ ] Import into org with partially overlapping data skips existing records
- [ ] Network/auth failure during modulo-migrate export produces non-zero exit
- [ ] Binary/blob data in table columns serialized as hex strings
- [ ] Empty output directory creates it before writing

## Known Gaps

- No unit tests for either CLI implementation
- No BDD feature files for migration/export behaviour
- modulo-migrate requires auth token or admin secret — no interactive login
- modulo (argparse) has no auth — runs with direct DB access
- No import conflict resolution for audit events (append-only constraint)
- No data validation before import — corrupt JSONL/JSON is accepted
