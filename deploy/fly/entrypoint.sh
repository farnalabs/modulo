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
# Ensure alembic_version can hold branch migration IDs (VARCHAR(255))
.venv/bin/python3 -c "
import os
os.environ['DATABASE_URL'] = os.environ.get('DATABASE_URL', '')
from modulo.db.session import AsyncSessionLocal
from sqlalchemy import text
import asyncio
async def fix():
    async with AsyncSessionLocal() as s:
        async with s.begin():
            await s.execute(text('CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(255) NOT NULL PRIMARY KEY)'))
            await s.execute(text(\"DELETE FROM alembic_version WHERE version_num = '0037_agent_columns'\"))
asyncio.run(fix())
" 2>&1 || echo "WARNING: Alembic prep step failed — continuing anyway"
.venv/bin/alembic upgrade heads || echo "WARNING: Migration failed — continuing anyway"

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
" 2>&1 || echo "WARNING: Schema patch step failed — continuing anyway"

echo "=== Ensuring app.modulo.run admin user exists ==="
.venv/bin/python3 -c "
import os
os.environ['DATABASE_URL'] = os.environ.get('DATABASE_URL', '')
from modulo.db.session import AsyncSessionLocal
from modulo.auth.passwords import hash_password
from sqlalchemy import select, text
import asyncio
import uuid
async def fix():
    async with AsyncSessionLocal() as s:
        async with s.begin():
            for email, role in [('admin@modulo.run', 'admin'), ('admin', 'admin')]:
                r = await s.execute(select(text('id')).select_from(text('users')).where(text(f\"email = '{email}'\")))
                row = r.one_or_none()
                pw = hash_password('admin123')
                if row:
                    await s.execute(text(f\"UPDATE users SET password_hash = :pw, org_role = :role WHERE email = :email\"), {'pw': pw, 'role': role, 'email': email})
                    print(f'{email} updated with known password + admin role')
                else:
                    org_r = await s.execute(select(text('id')).select_from(text('organisations')).order_by(text('created_at')).limit(1))
                    org_row = org_r.one_or_none()
                    if org_row:
                        await s.execute(text(f\"INSERT INTO users (id, organisation_id, email, display_name, password_hash, org_role, auth_provider) VALUES (:id, :oid, :email, :disp, :pw, :role, 'local')\"), {'id': uuid.uuid4(), 'oid': org_row[0], 'email': email, 'disp': email.split('@')[0], 'pw': pw, 'role': role})
                        print(f'{email} created with admin role')
                    else:
                        print(f'No org found — skipping {email}')
asyncio.run(fix())
" 2>&1 || echo "WARNING: Admin password fix failed — continuing anyway"

echo "=== Starting uvicorn ==="
exec .venv/bin/uvicorn modulo.api.main:app \
    --host 0.0.0.0 --port 8000 \
    --proxy-headers \
    --timeout-keep-alive 30 \
    --timeout-graceful-shutdown 30 \
    --limit-concurrency 100
