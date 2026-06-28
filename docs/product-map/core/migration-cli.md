---
id: feat-core-migration-cli
prd: §6.2 Self-Hosted → SaaS Migration
delivery-tasks: [task-nv9-migration-cli]
bdd:
code:
  - backend/src/modulo/cli/migrate.py
  - backend/src/modulo/cli/migrate_org.py
depends-on: []
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
- [ ] Export includes SHA-256 bundle hash in `__meta__.hash`
- [ ] `import-org --input --org-id --conflict` imports from JSON
- [ ] File hash verified before import begins; mismatch aborts
- [ ] `--conflict skip` leaves existing records by name-field match (default)
- [ ] `--conflict overwrite` replaces existing records
- [ ] `--conflict rename` appends `_imported` suffix to conflicting names
- [ ] FK columns automatically remapped to new IDs on import
- [ ] Paginated export (500 rows per batch) for large datasets

### Edge cases
- [ ] Duplicate records on import with `skip` strategy leave originals untouched
- [ ] Rename strategy increments counter (`_imported`, `_imported_2`, ...) until unique
- [ ] Rows with DB errors during import continue (non-fatal) with error count
- [ ] Complex column types (set, Decimal, JSON) serialised correctly
- [ ] No credentials exported for connector instances (gap: migrate_org.py exports them)
- [ ] `--org-id` uses UUID format; invalid UUIDs rejected
- [ ] Output path can be relative or absolute

### PRD compliance
- [ ] `modulo export-org` command exists (§6.2)
- [ ] `modulo import-org` command exists (§6.2)
- [ ] Pipelines exported/imported
- [ ] Agents exported/imported (gap: migrate.py does not export agents)
- [ ] Schemas exported/imported (gap: migrate.py does not export schemas)
- [ ] Connector instance configs exported (gap: credentials not excluded in either impl)
- [ ] Library entries exported/imported
- [ ] Audit events exported/imported (gap: migrate_org.py does not export audit events)
- [ ] Import is idempotent
- [ ] Feature flag `migration_cli` defined in v1 tier
- [ ] CLI documented in `docs/deployment.md`

## Known Gaps
- Two separate implementations with different interfaces and formats — consolidate to one
- `migrate_org.py` has zero unit tests
- `migrate_org.py` exports connector credentials (violates PRD §6.2: credentials excluded)
- `migrate_org.py` has no auth at all
- No BDD feature files exist for migration CLI
- `modulo-migrate` click impl uses `--on-conflict` but `migrate_org.py` uses `--conflict`
- PRD §6.2 shows `--target-org-id` but both impls use `--org-id`
- PRD §6.2 shows `modulo` binary but click impl registers as `modulo-migrate`
- PRD §6.2 lists agents and schemas in export; migrate.py does not export them
- PRD §6.2 lists audit events in export; migrate_org.py does not export them
- `migrate.py` uses table whitelist approach (misses new tables); `migrate_org.py` generic (auto-includes all model tables)

