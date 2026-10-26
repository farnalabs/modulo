#!/bin/bash
set -e

# Fly.io [release] command — runs ONCE per deploy on a single instance BEFORE
# the new machines roll out. This is the single migrator: the boot-time
# migration race (every machine running `alembic upgrade heads` simultaneously,
# serialised by a Postgres advisory lock, losers FATALing) disappears because
# migrations happen exactly once, up front, before any machine boots.
#
# Mirrors the common bootstrap section of entrypoint.sh (bootstrap_db.py, fixed
# URLs, role bootstrap) so the release instance prepares the DB exactly like an
# app boot would, then applies migrations with a bounded retry loop.

export PYTHONPATH="/app/src:${PYTHONPATH:-}"

echo "=== Release: single-migrator bootstrap + migrations ==="

# Fix DATABASE_URL and create alembic_version (same as entrypoint).
python3 /app/deploy/fly/bootstrap_db.py

if [[ -f /tmp/database_url.env ]]; then
  export DATABASE_URL="$(cat /tmp/database_url.env)"
fi

if [[ -f /tmp/database_admin_url.env ]]; then
  export DATABASE_ADMIN_URL="$(cat /tmp/database_admin_url.env)"
fi

# Create the modulo_app role if missing (non-fatal on failure).
python3 -m modulo.db.bootstrap_role || echo "  WARNING: role bootstrap failed (non-fatal)"

# Run migrations ONCE with a bounded retry loop (3 attempts, 5s apart).
#
# DISPOSITION (FAR-694, explicit): the migrator is bare-FATAL after its 3
# attempts — it does NOT implement the design doc's "skip-on-unknown-revision"
# invariant (a DB at a revision this image's chain does not know, i.e. the
# DB is AHEAD of the image). bare-FATAL was chosen deliberately: a
# newer-DB/older-image deploy is a broken deploy matrix that the deploy
# workflow's build-SHA match is supposed to prevent, and by the time
# migration 0215 (FAR-583) has dropped the runs blob columns, "skipping"
# migrations to let an older image boot would hide a schema the image cannot
# safely serve (the drop is never rewound). Failing loudly here is the safe
# default; the rollback path for an image rollback is the documented
# emergency re-add-columns snippet (migration 0215's downgrade docstring +
# docs/operations/drop-runs-blob-columns.md), never a revision skip.
MIGRATIONS_OK=0
for attempt in $(seq 1 3); do
    if alembic upgrade heads; then
        echo "  Migrations complete (attempt $attempt)"
        MIGRATIONS_OK=1
        break
    fi
    echo "  WARNING: migrations failed (attempt $attempt/3) -- retrying in 5s"
    sleep 5
done
if [[ "$MIGRATIONS_OK" -ne 1 ]]; then
    echo "FATAL: release migrations failed after 3 attempts" >&2
    exit 1
fi

# Publish migration completion event to Redis for health check awareness.
# The event includes the applied revision, timestamp, and machine ID.
# Best-effort: a Redis failure must not crash the release command.
echo "=== Publishing migration completion event ==="
python3 -c "
import os
import json
import redis
from datetime import datetime, UTC

# Get the applied head revision
from alembic.config import Config
from alembic.script import ScriptDirectory
alembic_ini = '/app/alembic.ini'
alembic_cfg = Config(alembic_ini)
alembic_cfg.set_main_option('sqlalchemy.url', os.environ.get('DATABASE_URL', ''))
alembic_cfg.set_main_option('script_location', '/app/src/modulo/db/migrations')
script = ScriptDirectory.from_config(alembic_cfg)
heads = set(script.get_heads())
revision = sorted(heads)[-1] if heads else 'unknown'

# Publish to Redis
redis_url = os.environ.get('REDIS_URL', 'redis://localhost:6379')
try:
    r = redis.from_url(redis_url, socket_connect_timeout=5)
    org_id = os.environ.get('MODULO_ORG_ID', 'system')
    machine_id = os.environ.get('FLY_MACHINE_ID', os.environ.get('HOSTNAME', 'unknown'))
    event = {
        'revision': revision,
        'timestamp': datetime.now(UTC).isoformat(),
        'machine_id': machine_id
    }
    r.set(f'saq:event:migration_completed:{org_id}', json.dumps(event), ex=3600)
    r.close()
    print(f'  Migration event published: revision={revision}')
except Exception as e:
    print(f'  WARNING: failed to publish migration event: {e}')
" || echo "  WARNING: migration event publish failed (non-fatal)"

# Wait for all app machines to report the new image before the rolling swap.
# This prevents a migration-skew window where old-image machines serve
# against a migrated DB schema.
echo "=== Waiting for all app machines to roll to new image ==="
NEW_IMAGE_REF="${FLY_IMAGE_REF:-$(flyctl image show --app app-modulo --json 2>/dev/null | jq -r '.[0].ref // empty' 2>/dev/null || echo '')}"
if [[ -n "$NEW_IMAGE_REF" ]]; then
    echo "  Target image: $NEW_IMAGE_REF"
    for i in $(seq 1 60); do
        # Get all app machine statuses
        MACHINE_JSON=$(flyctl machine list --app app-modulo --json 2>/dev/null || echo '[]')
        if [[ "$MACHINE_JSON" == "[]" ]]; then
            echo "  No machines found, retrying..."
            sleep 5
            continue
        fi
        ALL_ON_NEW=$(echo "$MACHINE_JSON" | jq -r --arg ref "$NEW_IMAGE_REF" '
            .[] | select(.state == "started" or .state == "created") | .config.image // .image_ref // ""
            | all(. == $ref)
        ' 2>/dev/null || echo "false")
        if [[ "$ALL_ON_NEW" == "true" ]]; then
            echo "  All app machines are on the new image"
            break
        fi
        if [[ $i -eq 60 ]]; then
            echo "  WARNING: timeout waiting for image rollout, proceeding anyway" >&2
            break
        fi
        echo "  Waiting for image rollout... ($i/60)"
        sleep 5
    done
else
    echo "  WARNING: could not determine new image ref, skipping rollout wait" >&2
fi

exit 0
