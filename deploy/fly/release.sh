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
