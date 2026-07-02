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
import asyncio
import uuid
from sqlalchemy import select
from modulo.db.session import AsyncSessionLocal
from modulo.auth.passwords import hash_password
from modulo.db.models.account import Account
from modulo.db.models.organisation import Organisation
from modulo.db.models.org_membership import OrgMembership

async def fix():
    async with AsyncSessionLocal() as s:
        async with s.begin():
            org_r = await s.execute(select(Organisation).order_by(Organisation.created_at).limit(1))
            org = org_r.scalar_one_or_none()
            if org is None:
                print('No organisation found — cannot create admin users')
                return

            for email, role in [('admin@modulo.run', 'admin'), ('admin', 'admin')]:
                r = await s.execute(select(Account).where(Account.email == email))
                account = r.scalar_one_or_none()
                pw = hash_password('admin123')
                if account:
                    account.password_hash = pw
                    if not account.active:
                        account.active = True
                    print(f'{email} updated with known password')
                else:
                    display_name = email.split('@')[0]
                    account = Account(
                        id=uuid.uuid4(),
                        email=email,
                        display_name=display_name,
                        password_hash=pw,
                        auth_provider='local',
                    )
                    s.add(account)
                    await s.flush()
                    print(f'{email} created')

                mr = await s.execute(
                    select(OrgMembership).where(
                        OrgMembership.account_id == account.id,
                        OrgMembership.organisation_id == org.id,
                    )
                )
                if not mr.scalar_one_or_none():
                    s.add(OrgMembership(
                        account_id=account.id,
                        organisation_id=org.id,
                        role=role,
                    ))
                    print(f'{email} linked to org as {role}')
asyncio.run(fix())
" 2>&1 || echo "WARNING: Admin password fix failed — continuing anyway"

echo "=== Starting uvicorn ==="
exec .venv/bin/uvicorn modulo.api.main:app \
    --host 0.0.0.0 --port 8000 \
    --proxy-headers \
    --timeout-keep-alive 30 \
    --timeout-graceful-shutdown 30 \
    --limit-concurrency 100
