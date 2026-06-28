#!/bin/sh
set -e

echo "=== Starting nginx ==="
nginx -g "daemon off;" &

echo "=== Bootstrap: fix DATABASE_URL and create alembic_version ==="
.venv/bin/python3 /app/deploy/fly/bootstrap_db.py

# Read the fixed URL from the bootstrap script's output file
if [ -f /tmp/database_url.env ]; then
  FIXED_URL=$(cat /tmp/database_url.env)
  export DATABASE_URL="$FIXED_URL"
  echo "DATABASE_URL fixed: $(echo $DATABASE_URL | cut -c1-80)..."
fi

echo "=== Running DB migrations ==="
.venv/bin/alembic upgrade head || echo "WARNING: Migration failed — continuing anyway"

if [ "$MODULO_DEMO_MODE" = "true" ]; then
  echo "=== Seeding demo data (idempotent) ==="
  cd /app
  .venv/bin/python3 /app/scripts/seed.py || echo "WARNING: Seed script failed — continuing anyway"
  echo "=== Seed complete ==="
fi

echo "=== Starting uvicorn ==="
exec .venv/bin/uvicorn modulo.api.main:app --host 0.0.0.0 --port 8000 --proxy-headers
