---
id: feat-core-backup-restore
prd: 6.2
delivery-tasks: [task-nv12-backup-restore]
code:
  - backend/src/modulo/cli/backup.py
bdd: []
depends-on: [feat-core-db-abstraction-core, feat-core-secrets-backend]
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
- [x] Backup writes `backup-info.json` manifest with timestamp, type, db_version, schema_versions, fernet_key_hash, file_checksums
- [x] Backup computes SHA-256 checksum for each output file and stores in manifest
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
- [x] Backup failure cleans up partial output directory (removes incomplete backup)
- [x] Backup manifest includes `file_checksums` dict with SHA-256 of every output file
- [x] Restore verifies all file checksums before making any DB changes
- [x] Restore pre-validates all JSON files (JSONDecodeError) before starting restore
- [x] Restore skips checksum verification for old backups (manifest without `file_checksums`)
- [x] Restore with missing manifest file raises clear error (listed but not on disk)
- [x] Restore with checksum mismatch raises clear error before any DB change

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
- [x] Invalid UUID in checkpoints JSON on restore → RuntimeError with context
- [x] Invalid UUID in checkpoint_writes JSON on restore → RuntimeError with context
- [x] Corrupt JSON file on restore (JSONDecodeError) → ClickException before any DB change
- [x] Backup file checksum mismatch on restore → ClickException before any DB change
- [x] Backup file listed in manifest but missing on disk → ClickException

## Known Gaps

- No BDD feature files for backup/restore behaviour (operational CLI — BDD may not apply)
- Restore assumes the same DB version — no cross-version compatibility check
- No encryption verification step after credential restoration
- No dry-run mode for restore preview
- Restore sequence is non-transactional across steps (psql → checkpoints → credentials) — a failure mid-sequence leaves DB in partially-restored state
- All checkpoint data is loaded into memory (list[dict]) — large orgs with millions of checkpoints may OOM; no streaming/batched export
- Restore uses individual INSERT statements per row, not COPY — slow for large datasets

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

**Status:** partial (7 known gaps remain)

### 2026-07-07 — Cross-cutting QA (index 317)

**CRITICAL fixes applied:**
- Backup files now have SHA-256 checksums stored in manifest (`file_checksums`). Restore verifies all checksums before making any DB change, catching silent corruption early.
- Restore pre-validates all JSON files (try `json.loads`) before any DB operation — corrupt JSON triggers clean ClickException before any data is changed.
- Unhandled `ValueError` in `_restore_checkpoints_sync` and `_restore_checkpoint_writes_sync` UUID parsing fixed — now raises `RuntimeError` with row context (matching `_restore_checkpoint_blobs_sync` pattern).
- Backup failure now cleans up partial output directory via `shutil.rmtree` instead of leaving incomplete files.
- Added 13 new unit tests: `_file_checksum` helper (2), restore integrity verification with checksums (3), restore skips checksums for old backups (1), restore fails on corrupt JSON (1), UUID error paths for checkpoints and checkpoint_writes (2).
- Product map: added 9 new behaviour checkboxes ([x]), 7 new error path checkboxes ([x]), 2 new Known Gaps.

**Status:** partial (7 known gaps remain)

### 2026-07-12 — R2 improve-architecture

- Removed dead code: redundant `if table not in _CREDENTIALS_TABLES` guard inside `_export_credentials_references_sync` (the loop already iterates over `_CREDENTIALS_TABLES`, so the check was always True).
- Fixed `E402` import order: moved module docstring before `from __future__ import annotations` to comply with PEP 8.
- Verified B904 compliance (all re-raises use `from exc`), no CancelledError concerns (sync CLI code), frontmatter clean, known gaps genuine.
- All ruff checks pass.

### 2026-07-12 — R3 improve-architecture

- Removed dead code: redundant `if table not in _CREDENTIALS_TABLES` guard inside `_re_encrypt_credentials_sync` (outer loop already skips unknown tables with `continue` at line 373, so the inner check was never reachable).
- Verified B904 compliance for all 4 entries, exc_info=True patterns, frontmatter integrity, and known gaps. No other issues found.
- All ruff checks pass.
