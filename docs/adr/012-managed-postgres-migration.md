# ADR 012: Migrate to Managed Fly Postgres

**Status:** Proposed — implementation deferred until production data warrants backups.

**Context:** All 3 database clusters (modulo-app-db, modulo-demo-db, modulo-staging-db) use
unmanaged Fly Postgres (Flex). On 2026-07-04, `modulo-app-db` PostgreSQL crashed and stayed
down for ~24h because there is no auto-restart mechanism — the `postgres` process simply
stopped while `repmgrd` and the monitoring agent kept running. Recovery required manual SSH
and `pg_ctl start`.

## Decision

Park migration until the project has meaningful production data. The current databases are
small, the data is easily reproducible, and the cost of managed Postgres (~$15-50/mo each)
is not justified at this stage.

## Migration Plan (for when we do it)

### Prerequisites

- Install `flyctl` with `fly mpg` plugin: `fly mpg create`
- Know the current DB credentials (in Fly secrets or `fly.toml`)
- Schedule a maintenance window (downtime ~30 min per DB)

### Step 1: Create Managed Postgres clusters

```sh
# For each environment (app, demo, staging):
fly mpg create --name modulo-app-db-mg --region lhr --initial-cluster-size 1 \
  --vm-size shared-cpu-1x --volume-size 10
```

Use the same region as the apps (lhr). Start with 1 node and 10GB volume.

### Step 2: Dump each database

```sh
# From a local dev machine with fly wireguard connected:
fly ssh console --app modulo-app-db -C "pg_dump -Fc -h localhost -U app_modulo -d app_modulo" > app_modulo.dump
# Same for demo and staging DBs
```

For demo: `-U demo_modulo -d demo_modulo`
For staging: `-U staging_modulo -d staging_modulo`

### Step 3: Restore into managed cluster

```sh
# Get the connection string for the new managed DB:
fly mpg connect --app modulo-app-db-mg --print-url

# Restore:
pg_restore -Fc --no-owner --dbname="<managed-connection-string>" app_modulo.dump
```

### Step 4: Update app secrets

```sh
# Update DATABASE_URL in each Fly app:
fly secrets set DATABASE_URL="<managed-connection-string>" --app app-modulo
fly deploy --app app-modulo
```

### Step 5: Verify

```sh
# Check health:
curl https://app.modulo.run/healthz
curl https://app.modulo.run/api/v1/deployment

# Verify alembic_version is correct:
fly ssh console --app app-modulo -C ".venv/bin/alembic current"
```

### Step 6: Destroy old clusters (after soak period)

After 1 week of successful operation:
```sh
fly apps destroy modulo-app-db
fly apps destroy modulo-demo-db
fly apps destroy modulo-staging-db
```

### Rollback

If managed DB has issues, switch back to unmanaged by updating `DATABASE_URL` to the
original flycast address (stored in 1Password or previous fly.toml configs). The unmanaged
clusters should not be deleted until the soak period expires.

## Cost Comparison

| DB | Unmanaged | Managed (shared-cpu-1x, 10GB) |
|---|---|---|
| app | Included in plan compute | ~$17/mo |
| demo | Included | ~$17/mo |
| staging | Included | ~$17/mo |
| **Total** | **$0/mo** | **~$51/mo** |

## Links

- Delivery tasks: `task-fly-postgres-migration`, `task-fly-postgres-docs`, `task-fly-postgres-monitoring`
- Incident: 2026-07-04 modulo-app-db crash (recovered via `pg_ctl start` over SSH)
