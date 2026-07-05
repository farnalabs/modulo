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
# Clean orphaned alembic_version entries (from restructured migration branches)
# then run all pending migrations.
.venv/bin/python3 -c "
import os, re
os.environ['DATABASE_URL'] = os.environ.get('DATABASE_URL', '')
from modulo.db.session import AsyncSessionLocal
from sqlalchemy import text
import asyncio
import importlib, pkgutil, pathlib

async def fix():
    # Collect all known revision IDs from migration files
    known = set()
    mod_path = pathlib.Path(os.environ.get('MODULO_ROOT', '/app')) / 'src' / 'modulo' / 'db' / 'migrations' / 'versions'
    for f in mod_path.glob('*.py'):
        m = re.search(r'^revision:\s*str\s*=\s*\"([^\"]+)\"', f.read_text(encoding='utf-8'), re.MULTILINE)
        if m:
            known.add(m.group(1))

    async with AsyncSessionLocal() as s:
        async with s.begin():
            await s.execute(text('CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(255) NOT NULL PRIMARY KEY)'))
            result = await s.execute(text('SELECT version_num FROM alembic_version'))
            db_versions = [row[0] for row in result.fetchall()]
            orphans = [v for v in db_versions if v not in known]
            for orphan in orphans:
                await s.execute(text(f\"DELETE FROM alembic_version WHERE version_num = '{orphan}'\")
                print(f'  Removed orphan alembic_version: {orphan}')
            if not orphans:
                print('  No orphaned alembic_version entries found')
asyncio.run(fix())
" 2>&1
.venv/bin/alembic upgrade heads

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
