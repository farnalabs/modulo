# Disaster Recovery — Modulo Self-Hosted

This document covers backup and restore procedures for self-hosted Modulo
instances using the `modulo backup` / `modulo restore` CLI.

---

## Prerequisites

- PostgreSQL client tools installed on the backup host:
  - `pg_dump` (≥ 16) — for creating SQL dumps
  - `psql` (≥ 16) — for restoring SQL dumps
- Network access to the Postgres database from the backup host
- A valid `DATABASE_URL` configured in the environment or `.env` file
- `psycopg-binary` Python package (installed as a Modulo dependency)

Verify the tools are available:

```bash
pg_dump --version
psql --version
```

---

## How to Take a Backup

### Basic backup

```bash
cd /path/to/modulo/backend
modulo backup
```

Creates `./modulo-backup-<YYYYMMDD-HHMMSS>/` containing:
| File | Contents |
|---|---|
| `database.sql` | Full Postgres dump (`pg_dump --clean --if-exists`) |
| `checkpoint_blobs.json` | LangGraph checkpoint blob data |
| `credentials_references.json` | Encrypted credential ciphertext references |
| `backup-info.json` | Manifest with timestamp, DB version, schema versions, FERNET_KEY hash |

### Custom output directory

```bash
modulo backup --output-dir /mnt/backups/modulo-20250215
```

### Override database URL

```bash
modulo backup --db-url "postgresql://user:pass@remote-host:5432/modulo"
```

The `DATABASE_URL` environment variable is also respected.

---

## How to Restore

### Standard restore

```bash
modulo restore ./modulo-backup-20250215-143022
```

The command will:
1. Validate `backup-info.json` exists in the target directory
2. Prompt for confirmation (data loss warning)
3. Restore the full database schema and data via `psql`
4. Re-insert `checkpoint_blobs` from the JSON export
5. Re-encrypt credentials if `FERNET_KEY` has changed

### Skip confirmation prompt

```bash
modulo restore ./modulo-backup-20250215-143022 --yes
```

### Override database URL

```bash
modulo restore ./modulo-backup-20250215-143022 --db-url "postgresql://user:pass@new-host:5432/modulo"
```

### Restore with credential re-encryption

If the `FERNET_KEY` has changed since the backup was taken, you must provide
the previous key:

```bash
modulo restore ./modulo-backup-20250215-143022 --previous-fernet-key "<old-key>"
```

The restore will:
1. Decrypt each credential ciphertext with the old key
2. Re-encrypt with the current `FERNET_KEY`
3. Update `connector_instances` and `model_backends` records

---

## How to Verify Backup Integrity

### 1. Check the manifest

```bash
cat ./modulo-backup-*/backup-info.json
```

Verify the `backup_type` is `"full"` and `schema_versions` matches your
deployment's expected Alembic heads.

### 2. Verify the SQL dump

```bash
head -50 ./modulo-backup-*/database.sql
```

Should start with `-- PostgreSQL database dump` and contain `CREATE TABLE` /
`DROP TABLE IF EXISTS` statements.

### 3. Check checkpoint_blobs count

```bash
python3 -c "import json; d=json.load(open('checkpoint_blobs.json')); print(f'{len(d)} records')"
```

### 4. Re-import to a test database

For full confidence, restore the backup to a separate Postgres instance:

```bash
createdb modulo-test
modulo restore ./modulo-backup-* --db-url "postgresql://user:pass@localhost:5432/modulo-test" --yes
```

---

## Credential Re-encryption Notes

- Credentials are stored encrypted with `FERNET_KEY` using symmetric Fernet
  encryption.
- The `backup-info.json` manifest stores a SHA-256 hash (first 16 hex chars)
  of the `FERNET_KEY` that was in use at backup time.
- During restore, if the current `FERNET_KEY` hash differs from the backup's,
  re-encryption is required and `--previous-fernet-key` must be provided.
- If `FERNET_KEY` has not changed, credentials are restored as-is — no
  re-encryption is needed.
- Plaintext credentials never appear in the backup files on disk.

### Key rotation workflow

```bash
# 1. Back up with old key
modulo backup

# 2. Update FERNET_KEY in .env
# 3. Restore with re-encryption
modulo restore ./modulo-backup-* --previous-fernet-key "<old-key>"
```

---

## Recovery Scenarios

### Full restore (complete data loss)

1. Provision a new Postgres instance
2. Configure `DATABASE_URL` in `.env`
3. Run `modulo restore <backup-dir> --yes`
4. Start the Modulo API server
5. Verify login and pipeline execution

### Point-in-time recovery

The `modulo backup` command creates point-in-time snapshots. To recover to a
specific point:

1. Identify the relevant backup directory by timestamp
2. Restore that specific backup: `modulo restore ./modulo-backup-<timestamp>`
3. If the DB supports PITR (e.g. WAL archiving), apply WAL segments after the
   pg_dump restore

### Partial restore (checkpoint blobs only)

If only LangGraph checkpoint data was lost:

```bash
# Export checkpoint blobs from a backup
cp ./modulo-backup-*/checkpoint_blobs.json /tmp/

# Use the restore command against the live DB
modulo restore ./modulo-backup-* --yes
```

### Credential key rotation without full restore

To rotate `FERNET_KEY` without a full backup/restore cycle:

```bash
# 1. Take a pre-rotation backup
modulo backup --output-dir ./pre-rotation-backup

# 2. Update FERNET_KEY in .env
# 3. Restore only the credential re-encryption
modulo restore ./pre-rotation-backup --previous-fernet-key "<old-key>" --yes
```

---

## Troubleshooting

| Symptom | Likely cause | Resolution |
|---|---|---|
| `pg_dump not found` | Postgres client tools not installed | Install `postgresql-client` package |
| `psql: FATAL: database "modulo" does not exist` | No target database | `CREATE DATABASE modulo` |
| `FERNET_KEY has changed since backup` | Key rotation without restore | Provide `--previous-fernet-key` |
| `psql restore failed` | Incompatible Postgres version | Restore to same or newer Postgres version |
| Backup directory is empty | Command failed during write | Check disk space and permissions |
