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
5. Pack the dump, configuration, manifest, and checksums into a `.tar.gz`
6. Encrypt with AES-256-CBC (PBKDF2, 600K iterations)
7. Write the encrypted archive. If `--output` is omitted, the filename is `modulo-backup-{org_id}-{timestamp}.tar.gz.enc`.

### Options

| Flag               | Description                                      |
|--------------------|--------------------------------------------------|
| `--output`, `-o`   | Output file path                                 |
| `--passphrase`, `-p` | Encryption passphrase                         |
| `--db-url`         | Postgres connection URL (default: `DATABASE_URL`) |
| `--pg-dump`        | pg_dump executable path                          |
| `--min-disk-gb`    | Minimum free disk space in GB (default: 1)       |

### Environment Variables

- `DATABASE_URL` – Postgres connection string
- `MODULO_BACKUP_PASSPHRASE` – Encryption passphrase (if not using `--passphrase`)

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

The `--data-only` and `--full` modes replace the target database. Stop the
application and any workers before using either mode, and verify the target
database URL before confirming the restore.

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

## Run Data Retention Export

Separately from backup files, Modulo retains run data in the database. The
**Run Retention** admin page (`/admin/run-retention`, system-admin only)
exports the runs matching the current filter so they can be archived before
the **purge**, which permanently deletes terminal runs and their checkpoints.
The export is created by `POST /api/v1/admin/run-retention/export` and
downloaded from the page.

### Export wire contract

The export body is **NDJSON**: exactly one run record per line. The **final
line is `{"_complete":true}`**, and it is emitted only when the export stream
ran to completion.

- If `{"_complete":true}` is **absent**, the export was **truncated**. A
  failure after the stream has started still returns HTTP 200 (the first
  record has already primed a successful response). For a consumer that holds
  only the saved file, the terminating line is the only truncation signal -
  a partial file simply ends without it. For a live download there is a second
  observable signal: an interrupted transfer rejects the body read outright.
  Verify the terminating line before treating a saved file as a complete
  compliance record.
- A consumer that maps each line to a schema must **skip** the
  `{"_complete":true}` line rather than read it as a run record.
- The in-product download (`AdminRunRetentionView.vue`) handles all of this:
  it performs the terminating-line check and, when it is missing, still saves
  the file but shows a non-blocking warning that it may be incomplete; when
  the transfer is interrupted outright, no file is produced at all and the
  page reports the export explicitly as did-not-complete
  (`role="alert"`).

See the feature's contract line in `docs/product-map/system/runtime.md`.

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
