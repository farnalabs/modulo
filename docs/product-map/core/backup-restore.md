---
id: feat-core-backup-restore
prd: (no dedicated section found)
delivery-tasks: [task-nv12-backup-restore]
bdd: [] (no feature file)
code:
  - backend/src/modulo/cli/backup.py
depends-on: []
status: partial
---

# Backup & Restore

## Behaviours

### Backup (`modulo backup`)
- [ ] Backup creates timestamped directory (`modulo-backup-<YYYYMMDD-HHMMSS>`)
- [ ] Backup runs `pg_dump --clean --if-exists --no-owner --no-acl --format=plain` for SQL dump
- [ ] Backup exports `checkpoint_blobs` table via sync psycopg query to JSON
- [ ] Backup exports credential references (`connector_instances`, `model_backends`) to JSON
- [ ] Backup writes `backup-info.json` manifest with timestamp, type, db_version, schema_versions, fernet_key_hash
- [ ] Backup accepts `--db-url` override (falls back to `DATABASE_URL` env / settings)
- [ ] Backup accepts `--output-dir` override
- [ ] Backup resolves asyncpg/psycopg URI schemes to postgresql:// for native tools
- [ ] Backup reports each export step to stdout
- [ ] Backup prints total directory size on completion
- [ ] Backup raises ClickException on failure (wraps any exception)
- [ ] Backup serialises UUID, bytes, datetime in JSON output

### Restore (`modulo restore`)
- [ ] Restore validates `backup-info.json` exists in backup directory
- [ ] Restore reads and displays manifest metadata (timestamp, type, db_version, schema_versions)
- [ ] Restore shows confirmation prompt before overwriting database (skipped with `--yes`)
- [ ] Restore runs `psql -q -v ON_ERROR_STOP=1` with `database.sql` to restore schema and data
- [ ] Restore skips database SQL step if `database.sql` missing
- [ ] Restore truncates and re-inserts `checkpoint_blobs` from JSON export
- [ ] Restore skips checkpoint_blobs step if JSON missing
- [ ] Restore re-encrypts credentials with current FERNET_KEY when key hash differs
- [ ] Restore requires `--previous-fernet-key` if key changed (otherwise raises error)
- [ ] Restore skips re-encryption when FERNET_KEY unchanged

### CLI conventions
- [ ] Both commands resolve DB URL from `--db-url` arg then `DATABASE_URL` env then settings
- [ ] Error output goes to stderr via `click.echo(..., err=True)`

### Credential handling
- [ ] Backup exports `credentials_ciphertext` as hex string (no plaintext leak)
- [ ] Restore decrypts with old Fernet key, re-encrypts with new Fernet key
- [ ] Re-encryption counts reported per table
- [ ] UUID primary keys serialised as strings during backup/restore

### Unit tests - backup (`tests/unit/scripts/test_backup.py`)
- [ ] Passphrase resolved from argument
- [ ] Passphrase resolved from `MODULO_BACKUP_PASSPHRASE` env var
- [ ] DB URL resolved from argument
- [ ] DB URL resolved from `DATABASE_URL` env var
- [ ] DB URL missing raises SystemExit
- [ ] Secrets collected into `secrets.env` (FERNET_KEY, SECRET_KEY, DATABASE_URL)
- [ ] Manifest written with `tool`, `version`, `created_at`
- [ ] `hash_file` returns consistent SHA-256
- [ ] `write_checksums` writes correct SHA-256 lines
- [ ] `create_archive` packs files into valid tar.gz
- [ ] `encrypt_archive` encrypts with openssl AES-256-CBC and removes plaintext
- [ ] Encrypted archive decrypts back to original content

### Unit tests - restore (`tests/unit/scripts/test_restore.py`)
- [ ] Passphrase resolved from argument
- [ ] Passphrase resolved from env var
- [ ] Archive decrypt: missing input exits
- [ ] Extract archive returns file paths
- [ ] Extract archive produces real files on disk
- [ ] `read_checksums` reads 3 entries from valid backup
- [ ] `read_checksums` returns empty dict when missing
- [ ] `verify_hashes` passes on intact archive
- [ ] `verify_hashes` fails on tampered file
- [ ] DB URL from argument
- [ ] DB URL from env var
- [ ] DB URL missing raises SystemExit

## Known Gaps
- No BDD / Gherkin feature file for backup/restore
- No PRD section specifying backup/restore requirements
- No tests for `_run_pg_dump` / `_run_psql` subprocess calls (no pg/psql in CI)
- No tests for checkpoint blobs export/import round-trip
- No tests for credential re-encryption logic
- No tests for partial/incomplete backup dir restore
- No test for backup-failure rollback (e.g. pg_dump fails mid-backup)
- No edge-case tests: empty database, concurrent backup, disk-full during backup
- No tests for URL scheme stripping (asyncpg to postgresql)
- No integration/BDD coverage for the CLI commands end-to-end

