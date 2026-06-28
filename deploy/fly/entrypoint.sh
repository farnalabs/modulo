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
# Fix stale alembic_version entries from prior branches
.venv/bin/python3 -c "
import os
os.environ['DATABASE_URL'] = os.environ.get('DATABASE_URL', '')
from modulo.db.session import AsyncSessionLocal
from sqlalchemy import text
import asyncio
async def fix():
    async with AsyncSessionLocal() as s:
        async with s.begin():
            # Remove stale 0037 entry if present (branch migration, file missing on main)
            await s.execute(text(\"DELETE FROM alembic_version WHERE version_num = '0037_agent_columns'\"))
            # Add default_autonomy_level to pipelines if missing
            await s.execute(text(\"ALTER TABLE pipelines ADD COLUMN IF NOT EXISTS default_autonomy_level VARCHAR(30) DEFAULT 'manual_approval'\"))
            # Add agent columns if still missing
            await s.execute(text(\"ALTER TABLE agents ADD COLUMN IF NOT EXISTS max_input_length INTEGER\"))
            await s.execute(text(\"ALTER TABLE agents ADD COLUMN IF NOT EXISTS token_budget INTEGER\"))
            await s.execute(text(\"ALTER TABLE agents ADD COLUMN IF NOT EXISTS library_id UUID\"))
asyncio.run(fix())
" 2>&1 || echo "WARNING: Schema fix step failed — continuing anyway"
.venv/bin/alembic upgrade head || echo "WARNING: Migration failed — continuing anyway"

if [ "$MODULO_DEMO_MODE" = "true" ]; then
  echo "=== Seeding demo data (idempotent) ==="
  cd /app
  .venv/bin/python3 /app/scripts/seed.py || echo "WARNING: Seed script failed — continuing anyway"
  echo "=== Seed complete ==="
fi

echo "=== Starting uvicorn ==="
exec .venv/bin/uvicorn modulo.api.main:app --host 0.0.0.0 --port 8000 --proxy-headers
