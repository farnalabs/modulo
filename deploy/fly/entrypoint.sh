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
# Clean orphaned alembic_version entries from restructured migration branches.
# If orphans were found and removed, the migration chain diverges from the
# codebase — stamp the current head instead of running upgrade paths that
# would try to re-apply already-existing schema changes.
.venv/bin/python3 /app/deploy/fly/cleanup_orphan_migrations.py 2>&1
HAS_ORPHANS=$?
if [ "$HAS_ORPHANS" -eq 0 ]; then
  .venv/bin/alembic stamp head
  echo "  Stamped head (orphans were present — schema assumed up to date)"
else
  .venv/bin/alembic upgrade heads
fi

echo "=== Applying schema patches (columns missing from base migrations) ==="
.venv/bin/python3 -c "
import os
os.environ['DATABASE_URL'] = os.environ.get('DATABASE_URL', '')
from modulo.db.session import AsyncSessionLocal
from sqlalchemy import text
import asyncio
async def fix():
    async with AsyncSessionLocal() as s:
        async with s.begin():
            await s.execute(text(\"ALTER TABLE pipelines ADD COLUMN IF NOT EXISTS default_autonomy_level VARCHAR(30) DEFAULT 'manual_approval'\"))
            await s.execute(text(\"ALTER TABLE agents ADD COLUMN IF NOT EXISTS max_input_length INTEGER\"))
            await s.execute(text(\"ALTER TABLE agents ADD COLUMN IF NOT EXISTS token_budget INTEGER\"))
            await s.execute(text(\"ALTER TABLE agents ADD COLUMN IF NOT EXISTS library_id UUID\"))
            print('Schema patches applied')
asyncio.run(fix())
" 2>&1

echo "=== Admin user seeding handled by backend lifespan startup (_seed_modulo_users) ==="

echo "=== Starting uvicorn ==="
exec .venv/bin/uvicorn modulo.api.main:app \
    --host 0.0.0.0 --port 8000 \
    --proxy-headers \
    --timeout-keep-alive 30 \
    --timeout-graceful-shutdown 30 \
    --limit-concurrency 100
