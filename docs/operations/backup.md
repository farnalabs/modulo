# Backup & Restore

## Overview

Modulo provides CLI scripts for automated backup and restore of Postgres data,
Fernet keys, and configuration. Backups are encrypted with AES-256-CBC via
OpenSSL.

## Prerequisites

- Python 3.12+
- `uv` (package manager)
- `pg_dump` >= 16 (Postgres client)
- `pg_restore` >= 16 (Postgres client, restore only)
- `openssl` (encryption)
- `psql` (Postgres client, restore only)

These are typically installed via your system package manager or the
Postgres distribution.

## The `modulo` CLI (recommended)

The `modulo` console script (installed with the backend package) is the primary
backup/restore interface. It runs a real `pg_dump` of the whole database,
exports the LangGraph checkpoint tables to JSON, records encrypted credential
references, and writes a `backup-info.json` manifest with per-file SHA-256
checksums. It is the tool exercised by the automated backup/restore CI gate
(`.github/workflows/backup-restore-nightly.yml`), which runs the full
backup -> restore -> downgrade -> upgrade round-trip against a real Postgres.

### Back up

```bash
cd /opt/modulo/backend
uv run modulo backup --db-url "$DATABASE_URL" --output-dir /backups/2026-08-13
```

This writes a directory (default `./modulo-backup-<timestamp>-<suffix>`, or the
`--output-dir` you pass) containing:

| File | Contents |
|------|----------|
| `database.sql` | `pg_dump --clean --if-exists --no-owner --no-acl` SQL dump (schema + data) |
| `checkpoint_blobs.json`, `checkpoints.json`, `checkpoint_writes.json` | LangGraph checkpoint tables as JSON |
| `credentials_references.json` | Encrypted credential rows (`connector_instances`, `model_backends`) |
| `backup-info.json` | Manifest: timestamp, DB version, schema heads, `fernet_key_hash`, per-file checksums |

Options: `--db-url` (default: `DATABASE_URL`), `--output-dir` / `-o`.

### Restore

```bash
uv run modulo restore /backups/2026-08-13 --db-url "$DATABASE_URL" --yes
```

Validates every file's checksum against the manifest, restores `database.sql`
via `psql`, re-inserts the checkpoint tables from the JSON exports, and — if
`FERNET_KEY` changed since the backup — re-encrypts stored credentials, which
requires `--previous-fernet-key <old-key>`. Pass `--yes` to skip the
confirmation prompt.

Requirements: `pg_dump` and `psql` from the Postgres client tools on `PATH`,
with a client major version >= the server's major version.

> The `modulo backup` CLI and the `scripts/backup.py` encrypted-archive tool
> both run a real `pg_dump`; the CLI additionally exports checkpoint data and
> supports credential re-encryption on restore. Pick one strategy per
> deployment and always test your restore in a staging environment first.

## Backup

### Usage

```bash
cd /opt/modulo/codebase
uv run scripts/backup.py --output /backups/daily/modulo-backup-20260624.tar.gz.enc
```

The script will:
1. Prompt for an encryption passphrase (or read `MODULO_BACKUP_PASSPHRASE`)
2. Check available disk space
3. Dump Postgres schema + data via `pg_dump`
4. Collect `FERNET_KEY`, `SECRET_KEY`, and other env vars
5. Pack everything into a `.tar.gz`
6. Encrypt with AES-256-CBC (PBKDF2, 600K iterations)
7. Write output with filename `modulo-backup-{org_id}-{timestamp}.tar.gz.enc`

### Options

| Flag               | Description                                      |
|--------------------|--------------------------------------------------|
| `--output`, `-o`   | Output file path                                 |
| `--passphrase`, `-p` | Encryption passphrase                         |
| `--db-url`         | Postgres connection URL (default: `DATABASE_URL`) |
| `--pg-dump`        | pg_dump executable path                          |
| `--min-disk-gb`    | Minimum free disk space in GB (default: 1)       |

### Environment Variables

- `DATABASE_URL` — Postgres connection string
- `MODULO_BACKUP_PASSPHRASE` — Encryption passphrase (if not using `--passphrase`)

### Cron Job Template

```cron
0 2 * * * cd /opt/modulo/codebase && uv run scripts/backup.py --output /backups/daily/$(date +\%Y\%m\%d).tar.gz.enc
```

## Restore

### Usage

```bash
cd /opt/modulo/codebase
uv run scripts/restore.py --input /backups/daily/backup-20260624.tar.gz.enc --dry-run
uv run scripts/restore.py --input /backups/daily/backup-20260624.tar.gz.enc --full
```

### Options

| Flag               | Description                                      |
|--------------------|--------------------------------------------------|
| `--input`, `-i`    | Encrypted backup archive path (required)         |
| `--passphrase`, `-p` | Decryption passphrase                         |
| `--db-url`         | Postgres connection URL (default: `DATABASE_URL`) |
| `--pg-restore`     | pg_restore executable path                       |
| `--dry-run`        | Verify archive integrity without restoring       |
| `--full`           | Restore everything (data + config)               |
| `--data-only`      | Restore Postgres only                            |
| `--config-only`    | Restore config/keys only                         |

### What Happens

1. Decrypts the archive with AES-256-CBC
2. Extracts to a temporary directory
3. Verifies SHA-256 checksums for every file
4. Based on mode:
   - **Dry-run**: verify only
   - **Data-only**: drops existing database, recreates it, imports via `pg_restore`
   - **Config-only**: prints secrets.env contents for manual application
   - **Full**: both data and config restore
5. Cleans up the temporary directory

## Retention Policy

Backups are pruned with `backup-prune.py`:

| Period | Retention |
|--------|-----------|
| Daily  | 7 most recent |
| Weekly | 4 most recent (Sundays) |
| Monthly | 12 most recent (1st of month) |

### Prune Usage

```bash
uv run scripts/backup-prune.py --backup-dir /backups/daily --dry-run  # preview
uv run scripts/backup-prune.py --backup-dir /backups/daily            # execute
```

### Prune Cron

```cron
0 3 * * * cd /opt/modulo/codebase && uv run scripts/backup-prune.py --backup-dir /backups/daily
```

## Full Restoration Walkthrough

```bash
# 1. Verify the backup is intact
uv run scripts/restore.py --input /backups/daily/backup-20260624.tar.gz.enc --dry-run

# 2. Stop the application
systemctl stop modulo

# 3. Restore everything
uv run scripts/restore.py --input /backups/daily/backup-20260624.tar.gz.enc --full

# 4. Verify data integrity
#    Re-apply secrets.env values, then restart
systemctl start modulo

# 5. Check health
curl https://modulo.example.com/health
```

## Upgrade Path: Backup Before Migrate, Restore After

Every schema upgrade is a migration event — never run `alembic upgrade head`
without a backup you can restore from. See also
[`docs/upgrade-process.md`](../upgrade-process.md).

1. **Back up first** (safe while the app is running — `pg_dump` produces a
   consistent snapshot):
   ```bash
   uv run modulo backup --db-url "$DATABASE_URL" --output-dir /backups/pre-upgrade
   ```
2. **Upgrade**: deploy the new version. The app runs migrations on startup, or
   run them manually:
   ```bash
   uv run alembic upgrade head
   ```
3. **Verify the upgrade** before moving on:
   ```bash
   uv run alembic current          # should show the head revision
   curl http://localhost:8000/health
   ```
4. **If the upgrade breaks**, restore the pre-upgrade backup into a fresh
   database and point the app at it:
   ```bash
   uv run modulo restore /backups/pre-upgrade --db-url "$DATABASE_URL" --yes
   ```

Restoring the pre-upgrade dump also restores the pre-upgrade `alembic_version`,
so the next app boot re-runs migrations against the old schema.

### Downgrade caveats

`alembic downgrade -1` moves the schema back one revision, but:

- **Not every migration ships a working `downgrade()`.** Some are additive
  only, or include data transforms that cannot be reversed. Treat a downgrade
  as best-effort, never as the primary rollback path.
- **Downgrade does not restore data.** Anything removed or transformed by the
  upgrade's `upgrade()` is not reconstructed. If you need the old data back,
  restore from backup.
- **Prefer a forward-fix.** The supported rollback is a new migration that
  reverts the schema change, plus (if data was affected) restoring from a
  backup.
- **The CI gate proves downgrades run.** The nightly backup/restore gate
  (`.github/workflows/backup-restore-nightly.yml`, runnable on demand via
  `gh workflow run backup-restore-nightly.yml`) executes `alembic downgrade -1`
  then `alembic upgrade head` against a restored database and asserts data
  integrity afterwards — a migration whose downgrade is broken fails the gate.

## Disaster Recovery Guide

### Scenario: Database corruption

```bash
# Restore just Postgres from latest backup
uv run scripts/restore.py --input /backups/latest.tar.gz.enc --data-only
```

### Scenario: Full server loss

1. Provision new server with Postgres 16+
2. Install Python 3.12, uv, Postgres client tools, OpenSSL
3. Copy backup archive to server
4. Restore config first, then database:

```bash
uv run scripts/restore.py --input backup.tar.gz.enc --config-only  # print secrets
export FERNET_KEY=...
export SECRET_KEY=...
uv run scripts/restore.py --input backup.tar.gz.enc --data-only
```

### Scenario: Key rotation after restore

If restoring to a new environment, update `FERNET_KEY` and `SECRET_KEY` in
the application `.env` file. All existing encrypted data (connector
credentials, audit chains) will be re-encrypted on first access.
