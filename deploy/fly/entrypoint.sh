#!/bin/sh
set -e

echo "=== Starting nginx ==="
nginx -g "daemon off;" &
NGINX_PID=$!

# Fix DATABASE_URL using Python — the shell (dash) doesn't support
# advanced parameter expansion, but Python is always available.
export DATABASE_URL=$(.venv/bin/python3 -c "
import os
url = os.environ.get('DATABASE_URL', '')
url = url.replace('postgres://', 'postgresql+asyncpg://', 1)
url = url.replace('?sslmode=disable', '')
url = url.replace('&sslmode=disable', '')
print(url)
")

echo "=== Pre-creating alembic_version with VARCHAR(255) ==="
.venv/bin/python3 -c "
import asyncio, asyncpg, os
url = os.environ['DATABASE_URL'].replace('postgresql+asyncpg://', 'postgres://')
async def main():
    conn = await asyncpg.connect(url)
    await conn.execute('CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(255) NOT NULL PRIMARY KEY)')
    await conn.close()
asyncio.run(main())
print('alembic_version table ready')
" 2>&1 || echo "WARNING: Could not pre-create alembic_version"

echo "=== Running DB migrations ==="
.venv/bin/alembic upgrade head || echo "WARNING: Migration failed — continuing anyway"

echo "=== Starting uvicorn ==="
exec .venv/bin/uvicorn modulo.api.main:app --host 0.0.0.0 --port 8000 --proxy-headers
