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

exit 0
