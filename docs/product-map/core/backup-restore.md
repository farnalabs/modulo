---
id: feat-core-backup-restore
prd: 6.2
delivery-tasks: [task-nv12-backup-restore]
code:
  - backend/src/modulo/cli/backup.py
bdd: []
depends-on: []
unit-tests: []
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

- [ ] Backup exports `credentials_ciphertext` as hex string from `connector_instances` and `model_backends`
- [ ] Restore re-encrypts with current FERNET_KEY when key hash differs (key rotation)
- [ ] Restore requires `--previous-fernet-key` for decryption when key changed
- [ ] Restore skips re-encryption when keys match

### Edge cases

- [ ] Non-existent output directory is created
- [ ] Missing `database.sql` in backup directory skips SQL restore
- [ ] Missing checkpoint_blobs JSON skips blob restore
- [ ] Empty checkpoint_blobs table exports empty array
- [ ] Connection failure raises ClickException

## Known Gaps

- No unit tests for `backup.py` exist
- No BDD feature files for backup/restore behaviour
- Restore assumes the same DB version — no cross-version compatibility check
- No encryption verification step after credential restoration
- No dry-run mode for restore preview
