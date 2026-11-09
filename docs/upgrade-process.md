# Upgrade Process

How to upgrade an existing Modulo deployment with minimal downtime. Covers version upgrades, database migrations, and rollback procedures.

---

## Project Version Convention

The **single source of truth** for the product version is `backend/pyproject.toml` (`[project].version`). It is mirrored into:

| File | Field |
|------|-------|
| `backend/uv.lock` | `version` on the editable `farnalabs-modulo` package entry |
| `frontend/package.json` | `"version"` |
| `deploy/helm/modulo/Chart.yaml` | `version` and `appVersion` |

All of these files carry a plain numeric `X.Y.Z`. A pre-release suffix (`-rc1`) lives in the **git tag only** (e.g. `v0.4.0-rc1`) and is never written into the files.

Bump with `scripts/bump-version.py` (reads the current version from `backend/pyproject.toml`, writes every mirror):

```bash
python scripts/bump-version.py patch        # or: minor | major
python scripts/bump-version.py --set 1.2.3  # explicit version
```

The script fails loudly (non-zero exit, nothing written) if any mirror has drifted away from the source of truth or is already at the target version - it never reports a change it did not make. It writes `backend/uv.lock` directly (the editable `farnalabs-modulo` package entry), so no separate `uv lock` run is needed after a bump. The `check-uv-lock` pre-commit hook and the CI `UV lockfile freshness` job fail closed when `backend/uv.lock` is stale against `backend/pyproject.toml`; if the resolution itself changed, run `uv lock` in `backend/` and commit the result.

---

## Before You Start

1. **Read the release notes** for the target version – check for breaking changes, new required env vars, and deprecated features.
2. **Check the Alembic migration chain** – review what schema changes will be applied:
   ```bash
   uv run alembic history
   uv run alembic upgrade heads --sql  # preview SQL without applying
   ```
   Run these from `backend/` with `DATABASE_URL` (or the owner-role override
   `DATABASE_ADMIN_URL`) set in the process environment or in the `.env` file
   in `backend/`. `backend/alembic.ini` deliberately carries no default URL, so
   Alembic refuses with an explicit error when neither is configured rather
   than silently targeting a local database (FAR-2183).
3. **Back up the database** – always take a backup before upgrading
   (from the repository root - `scripts/` lives at the repo root, not in
   `backend/`):
   ```bash
   uv run scripts/backup.py --output /backups/pre-upgrade-$(date +%Y%m%d).tar.gz.enc
   ```
4. **Test in staging first** – apply the upgrade to a staging environment with a copy of production data.

---

## Upgrade Paths

### Native single-install (Linux and macOS)

The native single-install path supports the published Linux bundle and the
signed, notarized native macOS arm64 and x86_64 `.dmg` artifacts. Windows native installer support is Beta
scope, opt-in only (FAR-2337 / FAR-2338): the release pipeline builds the native
payload, compiles, signs (Azure Trusted Signing) and attests the installer, and
includes it in `SHA256SUMS` and the release manifest only when the Beta-only
`include_windows` input is explicitly enabled (it defaults to false). Publication only happens when a release runs, so no signed Windows
installer is published yet: there is no published native Windows upgrade path
today, and real-host validation is outstanding under FAR-2250. Linux uses
`scripts/install.sh`; macOS uses `scripts/macos/install.sh` and installs the
LaunchAgent seam from the app bundle. See the platform support matrix in
[`docs/system-requirements.md`](./system-requirements.md).

The native upgrade swaps the `current` symlink after a verified data
snapshot; migrations run through the new bundle's boot lifespan. On macOS, install the signed/notarized DMG matching the host architecture:

| Mac | Native artifact | Rosetta |
|-----|-----------------|---------|
| Apple Silicon | `Modulo-<version>-macos-arm64.dmg` | Not required |
| Intel | `Modulo-<version>-macos-x86_64.dmg` | Not required |

These are separate native bundles, not a universal artifact. The installer validates the mounted app's launcher architecture as well as the host architecture, so renaming a DMG cannot bypass the check. The Linux installer verifies the signed release manifest/checksum
chain before copying the app; the macOS installer does not read the release
manifest and instead verifies the DMG and app bundle through `hdiutil verify`
plus Apple Developer ID signing/notarization. Two entry points perform the same
swap:

- the published installer (`bash scripts/install.sh`), and
- the launcher command (`modulo upgrade <target-version>`).

`modulo upgrade` flow:

1. **Enforced pre-upgrade dump.** The bundled pg_dump writes a versioned
   snapshot dir under the data dir (`pre-upgrade-dump-<ts>/`) with a
   restore manifest, and verifies the dump is non-empty. A failed dump
   aborts the upgrade - NO binary swap happens without a verified
   snapshot. The dump needs the bundled Postgres reachable, so run the
   upgrade command while the stack is running.
2. Stop + swap. The data dir must not be locked for the swap; the command
   stops the stack, re-points `current` to the new bundle, then boots the
   new bundle's migrations and gates on health.
3. Failure output carries the snapshot path and the EXACT manual restore
   command - v1 does NOT auto-restore on a failed upgrade.

`--skip-backup` skips the enforced dump. This flag is the loud, explicit
acknowledgement that a verified snapshot already exists (the
stop-then-rerun flow used by the published installer); the upgrade
refuses when no snapshot is present.

### Signed-manifest provisioning state (honest note)

`modulo upgrade` and `scripts/install.sh` both refuse to install from an
unsigned or unverifiable release manifest (an ed25519 signature over the
manifest's exact bytes). The current production key, `modulo-2026-a`, is
provisioned in the shipped verifier and the release pipeline's
`BUNDLE_MANIFEST_SIGNING_KEY` GitHub Actions secret. The next rotation slot is
reserved and remains empty in both trust-store assemblies:
`backend/src/modulo/launcher/manifest.py` `_TRUST_ROWS` and the installer's
`TRUST_KEY_CURRENT_B64` / `TRUST_KEY_NEXT_B64` values in `scripts/install.sh`.

No `bundle-v*` release has been published yet, so this does not claim that a
signed bundle is already available or that the installer is currently usable.
When rotation begins, use this two-stage protocol; do not change only the
secret. First, provision the replacement public key under a new key id in the
reserved NEXT slot and ship that overlap while the old key remains in CURRENT.
For the promotion release, make one coordinated change before producing any
artifact: move the old public key to NEXT under its old id, move the replacement
public key to CURRENT under its new id, update the checked-in trust store and
current-key id (in both the verifier and installer), replace the
``BUNDLE_MANIFEST_SIGNING_KEY`` GitHub Actions secret with the matching new
private key, and update the release workflow's expected-key-id contract to the
new current id. Release signing always derives the envelope key id from the
checked-in CURRENT slot. Verify that promotion release before publishing it:
old releases continue to verify via NEXT and the new release verifies via
CURRENT. Merely changing the secret while CURRENT remains the old key creates
a mismatched envelope and is invalid. After the compatibility window, remove
the old key from NEXT in both trust-store assemblies. Keep private signing key
material in the GitHub Actions secret only; never commit it. These are manual,
keys-in-vault operations tracked as needs-human.


### Docker Compose

The production compose file pins the image through `TAG`; never upgrade by
silently changing a floating tag. Before touching a real deployment, rehearse
the complete matrix on a disposable host with the same release compose asset.
Run the harness from a Modulo checkout, pointing it at the extracted
`docker-compose.prod.yml` and its generated `.env`:

```bash
python3 scripts/verify_compose_upgrade_rollback.py \
  --compose-file /path/to/compose-bundle/docker-compose.prod.yml \
  --env-file /path/to/compose-bundle/.env \
  --old-tag v0.4.0 \
  --new-tag v0.5.0 \
  --evidence-dir evidence/compose-upgrade
```

The harness records host/OS/Python versions, every command and exit code,
stdout/stderr, image tags, readiness assertions, and final `RESULT`. It owns a
unique Compose project name and always runs `down --volumes --remove-orphans`
in a `finally` block. The release workflow runs this same command against the
extracted RC compose asset after the clean-host check, using the previous
published `bundle-v*` release as `--old-tag`; it uploads the evidence directory
as a workflow artifact and fails closed when no previous release exists. The
matrix is:

1. clean install with the old immutable image tag;
2. upgrade to the new immutable image tag and readiness check;
3. intentionally failed upgrade by replacing the app with an invalid
   `SECRET_KEY`, asserting readiness is no longer HTTP 200 and retaining the
   app logs;
4. rollback to the old immutable image tag; and
5. rollback verification with readiness and app logs.

The failed-upgrade leg is deliberately a real container replacement, not a
mocked command. A run is not evidence unless the evidence directory contains
`metadata.json`, `commands.jsonl`, the referenced stdout/stderr files, and a
`RESULT` line. Do not report Windows or macOS coverage from this Linux/Docker
matrix; macOS validation is recorded separately and Windows remains deferred.

For the operator upgrade itself:

```bash
# 1. Pull the exact new image named by TAG.
TAG=<new-version> docker compose -f deploy/compose/docker-compose.prod.yml pull modulo

# 2. Restart with that immutable image (runs migrations on startup).
TAG=<new-version> docker compose -f deploy/compose/docker-compose.prod.yml up -d modulo

# 3. Verify migration completed and readiness is healthy.
TAG=<new-version> docker compose -f deploy/compose/docker-compose.prod.yml logs modulo | grep -i alembic
curl http://localhost:80/healthz/ready
```

### Self-Hosted (Bare Metal / VM)

```bash
# 1. Pull latest code
git pull origin main

# 2. Update dependencies
cd backend
uv sync

# 3. Run migrations
uv run alembic upgrade heads

# 4. Restart the service
sudo systemctl restart modulo

# 5. Verify
sudo journalctl -u modulo -n 50 --no-pager | grep alembic
curl http://localhost:8000/healthz
```

---

## Migration Behavior

Migrations run automatically on backend startup:

1. The backend pod/process starts
2. It acquires a two-key PostgreSQL advisory lock `(72001, 1)` via `pg_try_advisory_lock`
3. It runs `alembic upgrade heads` (idempotent)
4. On success, it proceeds to serve traffic
5. On failure, it retries up to 5 times with backoff, then fails boot

The advisory lock prevents concurrent migrations across multiple replicas. The lock key (`72001, 1`) must not conflict with other applications sharing the same Postgres instance.

---

## Zero-Downtime Requirements

| Requirement | Docker Compose |
|-------------|---------------|
| Minimum replicas | 1 (brief downtime on restart) |
| Readiness probe | Manual check |
| Rolling update | Not supported (stop + start) |

Docker Compose self-hosted deployments stop the old container before
starting the new one, so a brief downtime occurs during upgrades. For
true zero-downtime, deploy via Fly.io, which performs rolling deployments
between machine groups.

---

## Rollback

### Native single-install rollback playbook

The manual playbook. Run it when either trigger fires:

**Triggers**
1. A reproducible data-loss report against a released `bundle-vX.Y.Z`
   (failed upgrade, dropped data after restore, anything where the
   release artifact is the proximate cause).
2. A failed release - the nightly canary red for that release, or the
   release smoke failing on the tagged artifact.

**Actions (in order)**
1. **Unlist the failing release.** `gh release delete <tag> --yes` (or
   make it a draft). Published release assets are immutable, so a bad
   artifact must be removed from the download surface, not replaced.
2. **Re-point the install one-liner.** The installer resolves
   `releases/latest`; unlisting the bad release is usually sufficient.
   When you need an EXPLICIT re-point, run the rollback-verify target:
   `gh workflow run "Native: release smoke + canary (bundle-v*)" -f rollback_version=bundle-v<good>`.
   This verifies the target release exists with its artifact servable.
   A manual dispatch MUST pass one of `rollback_version` or `canary_version`:
   an argument-free dispatch would run only the tracking-housekeeping job and
   verify nothing, so the workflow rejects it with an explicit error naming
   both inputs (FAR-2161). To smoke a published release rather than verify a
   rollback target, pass `-f canary_version=bundle-v<good>` instead.
3. **Cut a patch.** Branch, fix, tag the next `bundle-v*` patch version; the release
   smoke runs against the exact artifact before Merge Queue can bless it.
4. **Operator-side recovery.** Any operator already holding the bad
   bundle restores from their pre-upgrade snapshot: the upgrade refusal
   output prints the snapshot dir and the exact `modulo restore` command.

**Triggers rule:** if the canary is red and no customer report exists,
treat 1-3 as REQUIRED, not optional - an endangered release stays listed
only while it is being rolled forward.

**Version pin reference (Docker Compose production):**

| Variable | Purpose | Example |
|----------|---------|---------|
| `TAG` | Modulo image version (required, no default) | `v0.4.0` |

### Application Rollback

```bash
# Docker Compose – pin the previous image version and restart
docker compose -f deploy/compose/docker-compose.prod.yml stop modulo
docker tag ghcr.io/farnalabs/modulo:old ghcr.io/farnalabs/modulo:<previous-version>
TAG=<previous-version> docker compose -f deploy/compose/docker-compose.prod.yml up -d

# Self-hosted
git checkout <previous-tag>
uv sync
sudo systemctl restart modulo
```

### Database Rollback (Downgrade)

**Warning:** Rolling back the application does NOT revert database migrations. If the previous code expects an older schema:

#### Irreversible data-purge migrations

A database restore alone is not a durable rollback for a migration that permanently deletes data. Stop the purge image and hold any release or startup path that could boot it. Restore the database from the pre-purge backup, then roll the application image back to an image built before the purge migration before starting the application again. Otherwise, startup or release migration logic can apply the purge again as soon as it sees the restored pre-purge revision.

The order is mandatory: **stop or hold the purge image, restore the database, roll back the application image, then boot it**. Do not boot the purge image between the restore and the application rollback.

```bash
# Check current Alembic version
docker compose -f deploy/compose/docker-compose.prod.yml exec modulo uv run alembic current

# Preview the downgrade SQL
docker compose -f deploy/compose/docker-compose.prod.yml exec modulo uv run alembic downgrade --sql -1

# Downgrade (use with extreme caution – data loss possible)
docker compose -f deploy/compose/docker-compose.prod.yml exec modulo uv run alembic downgrade -1
```

**Prefer a forward-fix over downgrading.** Write a new migration that reverts the schema change rather than using `alembic downgrade`. Not all migrations include a `downgrade()` function.

If downgrade fails, restore from the pre-upgrade backup instead
(from the repository root):

```bash
uv run scripts/restore.py --input /backups/pre-upgrade-<date>.tar.gz.enc --full
```

---

## Config Changes Between Versions

When upgrading, check for changes to:

1. **New required environment variables** – the application refuses to start if missing
2. **Deprecated environment variables** – log warnings indicate removal in a future version
3. **Changed defaults** – review [`docs/configuration-reference.md`](./configuration-reference.md) for current defaults
4. **New service dependencies** – e.g., Redis becoming required for new features
5. **API changes** – breaking endpoint changes are documented in release notes
6. **Provider registration changes (FAR-996)** – Docker providers are now registered exclusively via `MODULO_DOCKER_HOST` or `DOCKER_HOST`. An unrelated `MODULO_RUNNER_*` variable (e.g. `MODULO_RUNNER_TEMPLATE_ID`) no longer registers the Docker provider. If your `runner_docker` pipelines were relying on a `MODULO_RUNNER_*` variable to register Docker, you must now set `MODULO_DOCKER_HOST` (or `DOCKER_HOST`) and restart. The error message on `ProviderNotConfiguredError` already names the required env var.

---

## Post-Upgrade Verification

- [ ] Backend `/healthz` returns 200
- [ ] API responds to authenticated requests
- [ ] Existing pipeline runs appear in the UI
- [ ] WebSocket connections establish successfully
- [ ] Rate limiting is functional
- [ ] Audit log chain is intact (verified via `GET /api/v1/admin/audit/verify`; requires the Team-tier `audit_viewer` feature plus `audit.manage`. The chain is recorded on every tier, but on Community the product offers no route to verify it)
- [ ] All env vars are set correctly (no deprecation warnings in logs)
- [ ] Frontend loads without errors (check browser console)
- [ ] Cross-origin requests work (CORS)

---

## Cross-Reference

| Topic | Document |
|-------|----------|
| System requirements (platform support matrix) | [`docs/system-requirements.md`](./system-requirements.md) |
| Deployment guide | [`docs/deployment.md`](./deployment.md) |
| Deployment security | [`docs/deployment-security.md`](./deployment-security.md) §7 |
| Backup & restore | [`docs/operations/backup.md`](./operations/backup.md) |
| Configuration reference | [`docs/configuration-reference.md`](./configuration-reference.md) |
| Troubleshooting | [`docs/troubleshooting.md`](./troubleshooting.md) |
| Public launch checklist | [`docs/public-launch-checklist.md`](./public-launch-checklist.md) |
