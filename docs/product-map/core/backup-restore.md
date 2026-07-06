---
id: feat-core-backup-restore
prd: 6.2
delivery-tasks: [task-nv12-backup-restore]
code:
  - backend/src/modulo/cli/backup.py
bdd: []
depends-on: []
unit-tests:
  - backend/tests/unit/cli/test_backup.py
status: partial
---

# Backup & Restore

## Behaviours

### Backup (`modulo backup`)

- [x] Backup creates timestamped directory (`modulo-backup-<YYYYMMDD-HHMMSS>`)
- [x] Backup runs `pg_dump --clean --if-exists --no-owner --no-acl --format=plain` for SQL dump
- [x] Backup exports `checkpoint_blobs` table via sync psycopg query to JSON
- [x] Backup exports `checkpoints` table via sync psycopg query to JSON
- [x] Backup exports `checkpoint_writes` table via sync psycopg query to JSON
- [x] Backup exports credential references (`connector_instances`, `model_backends`) to JSON
- [x] Backup writes `backup-info.json` manifest with timestamp, type, db_version, schema_versions, fernet_key_hash
- [x] Backup accepts `--db-url` override (falls back to `DATABASE_URL` env / settings)
- [x] Backup accepts `--output-dir` override
- [x] Backup resolves asyncpg/psycopg URI schemes to postgresql:// for native tools
- [x] Backup reports each export step to stdout
- [x] Backup prints total directory size on completion
- [x] Backup raises ClickException on failure (wraps any exception)
- [x] Backup serialises UUID, bytes, datetime in JSON output

### Restore (`modulo restore`)

- [x] Restore validates `backup-info.json` exists in backup directory
- [x] Restore reads and displays manifest metadata (timestamp, type, db_version, schema_versions)
- [x] Restore shows confirmation prompt before overwriting database (skipped with `--yes`)
- [x] Restore runs `psql -q -v ON_ERROR_STOP=1` with `database.sql` to restore schema and data
- [x] Restore skips database SQL step if `database.sql` missing
- [x] Restore truncates and re-inserts `checkpoint_blobs` from JSON export
- [x] Restore truncates and re-inserts `checkpoints` from JSON export
- [x] Restore truncates and re-inserts `checkpoint_writes` from JSON export
- [x] Restore skips checkpoint_blobs step if JSON missing
- [x] Restore skips checkpoints step if JSON missing
- [x] Restore skips checkpoint_writes step if JSON missing
- [x] Restore re-encrypts credentials with current FERNET_KEY when key hash differs
- [x] Restore requires `--previous-fernet-key` if key changed (otherwise raises error)
- [x] Restore skips re-encryption when FERNET_KEY unchanged

### CLI conventions

- [x] Both commands resolve DB URL from `--db-url` arg then `DATABASE_URL` env then settings
- [x] Error output goes to stderr via `click.echo(..., err=True)`

### Credential handling

- [x] Backup exports `credentials_ciphertext` as hex string from `connector_instances` and `model_backends`
- [x] Restore re-encrypts with current FERNET_KEY when key hash differs (key rotation)
- [x] Restore requires `--previous-fernet-key` for decryption when key changed
- [x] Restore skips re-encryption when keys match

### Edge cases

- [x] Non-existent output directory is created
- [x] Missing `database.sql` in backup directory skips SQL restore
- [x] Missing checkpoint_blobs JSON skips blob restore
- [x] Empty checkpoint_blobs table exports empty array
- [x] Connection failure raises ClickException

### Error paths

- [x] pg_dump not found (FileNotFoundError) → ClickException
- [x] pg_dump non-zero exit → RuntimeError → ClickException
- [x] psql not found (FileNotFoundError) → ClickException
- [x] psql non-zero exit → RuntimeError → ClickException
- [x] checkpoint_blobs table missing (ProgrammingError) → ClickException
- [x] checkpoints table missing (ProgrammingError) → ClickException
- [x] checkpoint_writes table missing (ProgrammingError) → ClickException
- [x] Credentials table missing (ProgrammingError) → ClickException
- [x] Restore with missing backup-info.json → ClickException
- [x] Restore with changed FERNET_KEY but no --previous-fernet-key → ClickException
- [x] Invalid FERNET_KEY during re-encryption (InvalidToken) → ClickException
- [x] Empty --previous-fernet-key passed (empty string) → Fernet error → ClickException

## Known Gaps

- No BDD feature files for backup/restore behaviour (operational CLI — BDD may not apply)
- Restore assumes the same DB version — no cross-version compatibility check
- No encryption verification step after credential restoration
- No dry-run mode for restore preview
- Restore sequence is non-transactional across steps (psql → checkpoints → credentials) — a failure mid-sequence leaves DB in partially-restored state

## QA History

- 2026-07-02: improve-architecture (index 45) — cross-cutting QA: marked all 33 behaviours [ ]→[x], added 10 error-path behaviour checkboxes, added 62 unit tests covering all functions and CLI commands, fixed pre-existing `User`→`Account` import bug in `migrate_org.py`. Status: partial (4 known gaps remain).

### 2026-07-06 — Cross-cutting QA (improve-architecture index 233)

**CRITICAL fixes applied:**
- Backup only exported `checkpoint_blobs` but LangGraph uses 3 checkpoint tables (`checkpoints`, `checkpoint_blobs`, `checkpoint_writes`). Restoring only checkpoint_blobs left stale data in the other two tables, causing metadata inconsistency after restore.
- Added `_export_checkpoints_sync()`, `_export_checkpoint_writes_sync()` export functions
- Added `_restore_checkpoints_sync()`, `_restore_checkpoint_writes_sync()` restore functions
- Wired into `backup()` and `restore()` CLI commands
- Added 17 new unit tests covering export/restore for both tables (including null handling, memoryview blobs, JSONB columns, truncate+insert pattern)
- Updated all 11 existing CLI backup/restore tests with new mock parameters and fixture files
- Product map: added 5 new behaviour checkboxes ([x]), 2 new error path checkboxes ([x]), 1 new Known Gap

**Status:** partial (5 known gaps remain)
