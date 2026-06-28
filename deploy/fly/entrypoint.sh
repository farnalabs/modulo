#!/bin/sh
set -e

echo "=== Starting nginx ==="
nginx -g "daemon off;" &
NGINX_PID=$!

# Fly attaches Postgres with DATABASE_URL=postgres://...?sslmode=disable
# SQLAlchemy async drivers need "postgresql+asyncpg://" scheme.
# asyncpg accepts ?ssl= but not ?sslmode= in the SQLAlchemy dialect wrapper.
RAW_URL="$DATABASE_URL"
FIXED_URL="$(echo "$RAW_URL" | sed 's|postgres://|postgresql+asyncpg://|')"
# Convert sslmode to ssl for asyncpg compatibility
FIXED_URL="$(echo "$FIXED_URL" | sed 's/sslmode=disable/ssl=0/g; s/sslmode=prefer/ssl=1/g; s/sslmode=require/ssl=1/g')"
export DATABASE_URL="$FIXED_URL"
echo "DATABASE_URL fixed for SQLAlchemy async driver"

echo "=== Pre-creating alembic_version with VARCHAR(255) ==="
# Branch migration IDs exceed VARCHAR(32). Must create the table with
# VARCHAR(255) before alembic does it automatically.
PG_URL="$(echo "$BASE_URL" | sed 's|postgresql+asyncpg://|postgres://|')"
.venv/bin/python3 -c "
import asyncio, asyncpg
url = '$PG_URL'
async def main():
    conn = await asyncpg.connect(url)
    await conn.execute('''
        CREATE TABLE IF NOT EXISTS alembic_version (
            version_num VARCHAR(255) NOT NULL PRIMARY KEY
        )
    ''')
    await conn.close()
asyncio.run(main())
print('alembic_version table ready')
" 2>&1 || echo "WARNING: Could not pre-create alembic_version"

echo "=== Running DB migrations ==="
.venv/bin/alembic upgrade head || echo "WARNING: Migration failed — continuing anyway"

echo "=== Starting uvicorn ==="
exec .venv/bin/uvicorn modulo.api.main:app --host 0.0.0.0 --port 8000 --proxy-headers
