# Upgrade Process

How to upgrade an existing Modulo deployment with minimal downtime. Covers version upgrades, database migrations, and rollback procedures.

---

## Before You Start

1. **Read the release notes** for the target version – check for breaking changes, new required env vars, and deprecated features.
2. **Check the Alembic migration chain** – review what schema changes will be applied:
   ```bash
   uv run alembic history
   uv run alembic upgrade heads --sql  # preview SQL without applying
   ```
3. **Back up the database** – always take a backup before upgrading:
   ```bash
   uv run scripts/backup.py --output /backups/pre-upgrade-$(date +%Y%m%d).tar.gz.enc
   ```
4. **Test in staging first** – apply the upgrade to a staging environment with a copy of production data.

---

## Upgrade Paths

### Native single-install (Linux)

The native upgrade swaps the `current` symlink after a verified data
snapshot; migrations run through the new bundle's boot lifespan. Two entry
points perform the same swap:

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
manifest's exact bytes). The trust store ships in a PROVISIONING state:
production signing keys are NOT yet provisioned (the release pipeline's
`BUNDLE_MANIFEST_SIGNING_KEY` Actions secret must be set, and the
installer's production trust slots `TRUST_KEY_CURRENT_B64` /
`TRUST_KEY_NEXT_B64` in `backend/src/modulo/launcher/manifest.py`
`_TRUST_ROWS` mirror it). Until a human provisions those keys, signed
releases cannot be produced and unsigned ones fail closed at install -
deliberate: an unsigned release must never install silently. Provisions
are a manual, keys-in-vault operation tracked as needs-human.

### Docker Compose

```bash
# 1. Pull the latest image
docker compose -f deploy/compose/docker-compose.prod.yml pull

# 2. Restart with new image (runs migrations on startup)
docker compose -f deploy/compose/docker-compose.prod.yml up -d

# 3. Verify migration completed
docker compose -f deploy/compose/docker-compose.prod.yml logs modulo | grep alembic

# 4. Check application health
curl http://localhost:8000/healthz
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

## Migration Behaviour

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
3. **Cut a patch.** Branch, fix, tag `bundle-v<somer+1>`; the release
   smoke runs against the exact artifact before Merge Queue can bless it.
4. **Operator-side recovery.** Any operator already holding the bad
   bundle restores from their pre-upgrade snapshot: the upgrade refusal
   output prints the snapshot dir and the exact `modulo restore` command.

**Triggers rule:** if the canary is red and no customer report exists,
treat 1-3 as REQUIRED, not optional - an endangered release stays listed
only while it is being rolled forward.

### Application Rollback

```bash
# Docker Compose – re-tag and restart
docker compose -f deploy/compose/docker-compose.prod.yml stop modulo
docker tag ghcr.io/farnalabs/modulo:old ghcr.io/farnalabs/modulo:latest
docker compose -f deploy/compose/docker-compose.prod.yml up -d

# Self-hosted
git checkout <previous-tag>
uv sync
sudo systemctl restart modulo
```

### Database Rollback (Downgrade)

**Warning:** Rolling back the application does NOT revert database migrations. If the previous code expects an older schema:

```bash
# Check current Alembic version
docker compose -f deploy/compose/docker-compose.prod.yml exec modulo uv run alembic current

# Preview the downgrade SQL
docker compose -f deploy/compose/docker-compose.prod.yml exec modulo uv run alembic downgrade --sql -1

# Downgrade (use with extreme caution – data loss possible)
docker compose -f deploy/compose/docker-compose.prod.yml exec modulo uv run alembic downgrade -1
```

**Prefer a forward-fix over downgrading.** Write a new migration that reverts the schema change rather than using `alembic downgrade`. Not all migrations include a `downgrade()` function.

If downgrade fails, restore from the pre-upgrade backup instead:

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

---

## Post-Upgrade Verification

- [ ] Backend `/healthz` returns 200
- [ ] API responds to authenticated requests
- [ ] Existing pipeline runs appear in the UI
- [ ] WebSocket connections establish successfully
- [ ] Rate limiting is functional
- [ ] Audit log chain is intact (verified via `GET /api/v1/admin/audit/verify`)
- [ ] All env vars are set correctly (no deprecation warnings in logs)
- [ ] Frontend loads without errors (check browser console)
- [ ] Cross-origin requests work (CORS)

---

## Cross-Reference

| Topic | Document |
|-------|----------|
| Deployment guide | [`docs/deployment.md`](./deployment.md) |
| Deployment security | [`docs/deployment-security.md`](./deployment-security.md) §7 |
| Backup & restore | [`docs/operations/backup.md`](./operations/backup.md) |
| Configuration reference | [`docs/configuration-reference.md`](./configuration-reference.md) |
| Troubleshooting | [`docs/troubleshooting.md`](./troubleshooting.md) |
| Public launch checklist | [`docs/public-launch-checklist.md`](./public-launch-checklist.md) |
