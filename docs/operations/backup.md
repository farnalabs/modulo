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

## Upgrade Path: Dump Before Migrating

A schema change is a migration event. Take a dump before running
`alembic upgrade heads`, so a bad upgrade has a recovery point that does not
depend on the migration itself.

### Which tool, which artefact

This page documents two backup tools. They produce different artefacts and are
**not interchangeable in either direction**:

| Tool | Produces | Restored by | Encrypted |
|------|----------|-------------|-----------|
| `scripts/backup.py` | one `modulo-backup-<org>-<timestamp>.tar.gz.enc` file | `scripts/restore.py --input` | Yes - AES-256-CBC (PBKDF2, 600K iterations) |
| `modulo backup` | a **directory** holding `database.sql`, the three checkpoint JSON exports, `credentials_references.json` and `backup-info.json` | `modulo restore <directory>` | **No** - the directory is written unencrypted |

`scripts/restore.py --input` reads only a `scripts/backup.py` archive, and
`modulo restore` reads only a directory written by `modulo backup`. The
version-upgrade procedure in
[`docs/upgrade-process.md`](../upgrade-process.md) uses the `scripts/backup.py`
/ `scripts/restore.py` pair - the encrypted archive - so that is the pair to
use when following that procedure. The `modulo backup` / `modulo restore` pair
described below is the in-product database-level dump; the AES-256-CBC claim in
the Overview applies to `scripts/backup.py` only, so store a `modulo backup`
directory on encrypted media if the archive itself must be encrypted.
`scripts/backup-prune.py` prunes only `modulo-backup-*.tar.gz.enc` files, so it
does NOT prune `modulo backup` directories - delete those yourself.

The `modulo` and `alembic` commands in this section run from `backend/`, where
`alembic.ini` and the `modulo` entry point live (step 1 changes into it).

### 1. Take the dump

```bash
cd /opt/modulo/backend
uv run modulo backup --db-url "$DATABASE_URL" \
  --output-dir "/backups/pre-upgrade-$(date +%Y%m%d-%H%M%S)"
```

Give every dump its own directory. `--output-dir` creates the directory when it
is missing but reuses it when it exists, and the files inside it are
overwritten - so a fixed path silently replaces the previous recovery point.
(Without `--output-dir`, `modulo backup` creates a fresh
`./modulo-backup-<timestamp>-<rand>` directory in the current directory.)

That writes `database.sql` (a `pg_dump` of schema and data), the three
LangGraph checkpoint JSON exports (`checkpoints.json`,
`checkpoint_blobs.json`, `checkpoint_writes.json`),
`credentials_references.json`, and a `backup-info.json` manifest recording the
schema head and a SHA-256 checksum per file. `pg_dump` and `psql` must be on
`PATH`, at the same major version as the server.

### 2. Upgrade and check the result

```bash
uv run alembic upgrade heads
uv run alembic current
```

`alembic current` prints the revision the database is recorded at, suffixed
`(head)` when it matches the chain head. Use `heads` (plural) to match the
single head the chain has today - `alembic heads` prints one revision.

### 3. If the upgrade breaks: restore the dump

Check the archive first, then restore it (substituting the timestamped
directory you dumped in step 1):

```bash
uv run modulo restore /backups/pre-upgrade-<timestamp> --db-url "$DATABASE_URL" --dry-run
uv run modulo restore /backups/pre-upgrade-<timestamp> --db-url "$DATABASE_URL" --yes
```

`restore` verifies every file against the manifest's checksums before it
touches the database; `--dry-run` performs those checks and stops. Because
`database.sql` is a dump of the whole database, restoring it also restores the
pre-upgrade `alembic_version` row, leaving the database recorded at the
revision it was dumped from.

Note the two different versions in play. The manifest's `schema_versions` field
records the **migration head of the code that wrote the backup** (`alembic`
script heads), not the database's `alembic_version` row. The two differ whenever
the dump is taken after deploying a new release but before
`alembic upgrade heads` runs. `modulo restore` refuses an archive whose
recorded versions are not a subset of the running code's heads - which, in a
linear chain, means **the restore must run the same release that wrote the
dump**:

- dump and restore with the same release. In the steps above that is the new
  release: the dump records the new release's head while the database inside it
  is still at the old revision;
- if the upgrade breaks, restore with the **new** binary first (that is what
  step 3 does), and only then roll the code back if you still need to;
- a dump taken by the **old** release before deploying can only be restored by
  the old release, so roll the code back first and restore afterwards.

**Exception for an irreversible data-purge migration:** do not boot or release
with an image that contains the purge after restoring its pre-purge database.
Stop or hold that image, restore the database, roll the application image back
to one built before the purge migration, and only then boot the application.
The startup and release migration paths can see the restored pre-purge revision
and apply the purge again if the purge image starts first.

### What `alembic downgrade -1` does, and what it does not

`alembic downgrade -1` moves the schema back exactly one revision. The nightly
gate runs it, then upgrades back to head, to prove the head migration's
`downgrade()` executes against a real, populated database:

```bash
uv run alembic downgrade -1
uv run alembic current        # the head's parent revision, not head
uv run alembic upgrade heads  # back to head
```

Two things it does not do:

- **It moves one revision, not "to the previous release".** A release can carry
  many migrations; getting back before it takes as many steps as it added, and
  reversing *data* changes is not possible.
- **It does not restore data.** Anything an `upgrade()` removed or rewrote is
  not reconstructed.

The gate therefore only proves that the head migration's downgrade works. The
recovery path for a bad upgrade is the dump, not the downgrade.

### What the nightly gate proves

`.github/workflows/backup-restore-nightly.yml` runs
`scripts/verify_backup_restore.py` at 04:30 UTC daily and on demand. Against a
throwaway Postgres 16 it:

1. runs `alembic upgrade heads` from an empty database and asserts the recorded
   revision is head;
2. seeds an organisation, account, membership, pipeline, snapshot and run
   through the ORM;
3. creates the LangGraph checkpoint tables exactly as app startup does and seeds
   one row in each of `checkpoints`, `checkpoint_blobs` and `checkpoint_writes`,
   so the checkpoint exports and restore path are not exercised on empty
   tables;
4. runs `modulo backup` and asserts all six artefacts were produced and that
   `database.sql` is non-empty;
5. runs `modulo restore` into a second, empty database and asserts the seeded
   rows and all three checkpoint rows are intact;
6. re-applies the promoted first-boot posture path with the real `modulo_app`
   role and asserts `NOBYPASSRLS` plus the expected schema, table and column
   grants (the posture is deliberately broken before the restore, so this
   assertion cannot pass vacuously);
7. runs `alembic downgrade -1` and asserts the recorded revision is the head's
   parent - a downgrade that was skipped fails the gate rather than passing
   silently;
8. runs `alembic upgrade heads` and asserts the revision is back at head, with
   the seeded rows still intact.

A `downgrade()` that cannot run, a dump that cannot be restored, or data lost
in either direction fails the job.

### Run the gate by hand

Run the script from the **repo root** - it lives at
`scripts/verify_backup_restore.py`, and `--project backend` resolves the backend
package from there. Running it from `backend/` fails to find the script.

The script creates and drops its own scratch databases (`<db>_backup_src` and
`<db>_restored`) on the server it is pointed at, and leaves the database named
in the URL untouched. It also **resets the runtime roles** the migration chain
needs (`modulo_app`, `modulo_migrate`, `modulo_breakglass`) with `DROP ROLE` /
`CREATE ROLE` and fixed passwords. Roles are **cluster-scoped, not
database-scoped**, so on a shared server that rotates the role passwords of
every deployment living on it. The script therefore refuses to start unless the
URL's host is loopback/private AND its database name ends in a scratch suffix
(`_test`, `_backup_src`, `_restored`, ...), or you pass `--allow-role-reset`,
which accepts that consequence deliberately. Only ever point it at a dedicated
throwaway server.

```bash
NAME=modulo-dr-gate-$(date +%Y%m%d-%H%M%S)
docker run -d --rm --name "$NAME" \
  -e POSTGRES_USER=modulo_test -e POSTGRES_PASSWORD=modulo_test \
  -e POSTGRES_DB=modulo_test -p 0:5432 postgres:16-alpine
PORT=$(docker port "$NAME" 5432/tcp | tail -1 | sed 's/.*://')

export DATABASE_URL=postgresql+asyncpg://modulo_test:modulo_test@localhost:$PORT/modulo_test
uv run --project backend python scripts/verify_backup_restore.py
```

Let Docker assign the host port and give the container a unique name: a fixed
`--name` / `-p 55555:5432` pair collides with any concurrent run, and a killed
run leaves both held.

`--database-url` overrides `$DATABASE_URL`. `--keep-databases` leaves the two
scratch databases and the backup artefacts behind for inspection. If `pg_dump`
or `psql` is missing from `PATH` the script exits non-zero naming the missing
tool; if `SECRET_KEY` or `FERNET_KEY` is unset it prints a notice and generates
a throwaway value for that run rather than requiring deployment secrets.

To run the gate on demand, use the workflow's **Run workflow** control in the
Actions tab - it declares `workflow_dispatch` for exactly that.

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

## `modulo restore`: database roles and posture

`modulo restore <backup-dir>` finishes by re-running the same first-boot role
bootstrap the application runs, so the restored cluster ends up with the
posture the application expects: the app role cannot bypass RLS, the system and
break-glass roles can, and every grant is re-applied.

- **Which role is the app role?** It is resolved from configuration, never from
  the restore target: `--app-db-url` if given, otherwise `DATABASE_URL` from
  settings. `--db-url` (and `DATABASE_ADMIN_URL`) is the privileged connection
  the restore itself writes through; using it as the app role would configure
  the admin role and leave the real app role untouched.
- **A posture failure fails the restore.** If the bootstrap raises, `modulo
  restore` exits non-zero and names the failing check. The data is already
  restored at that point, but the cluster must not serve traffic until the
  posture is fixed: correct the reported problem, then run
  `uv run python -m modulo.db.bootstrap_role` (with `DATABASE_ADMIN_URL` and
  `DATABASE_URL` set) before starting the application.
- **A restore into a cluster that uses a different app role** needs
  `--app-db-url` pointing at that role's connection URL.

`scripts/restore.py` (the encrypted-archive tool documented above) does not
perform this step; it applies the archive only.
