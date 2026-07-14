"""Run at container startup to prepare the database.
1. Fix DATABASE_ADMIN_URL for SQLAlchemy async driver
2. Create alembic_version table with VARCHAR(255) for branch migrations
3. Write the fixed DATABASE_ADMIN_URL to a file for the shell script
"""

import asyncio
import os
import re
import sys

import asyncpg

# Step 1: Fix DATABASE_ADMIN_URL and DATABASE_URL
admin_url = os.environ.get("DATABASE_ADMIN_URL") or os.environ.get("DATABASE_URL", "")
original = admin_url
admin_url = admin_url.replace("postgres://", "postgresql+asyncpg://", 1)
# Strip any sslmode parameter (disable, require, prefer, etc.) — asyncpg
# defaults to "prefer" (try SSL, fall back to plain) on Fly's private
# networks where Postgres doesn't expect SSL, causing ConnectionResetError.
admin_url = re.sub(r"[?&]sslmode=[^&]*", "", admin_url).rstrip("?")
os.environ["DATABASE_ADMIN_URL"] = admin_url
if admin_url != original:
    print("Fixed DATABASE_ADMIN_URL scheme + stripped sslmode")

# Also fix DATABASE_URL (the runtime URL) for backwards compat
runtime_url = os.environ.get("DATABASE_URL", "")
if runtime_url:
    runtime_url = runtime_url.replace("postgres://", "postgresql+asyncpg://", 1)
    runtime_url = re.sub(r"[?&]sslmode=[^&]*", "", runtime_url).rstrip("?")
    os.environ["DATABASE_URL"] = runtime_url

# Step 2: Create alembic_version table with VARCHAR(255)
# Branch migration IDs exceed the default VARCHAR(32).


async def _bootstrap():
    pg_url = admin_url.replace("postgresql+asyncpg://", "postgres://")
    conn = await asyncpg.connect(pg_url, ssl=False)
    try:
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS alembic_version ("
            "  version_num VARCHAR(255) NOT NULL PRIMARY KEY"
            ")"
        )
        print("alembic_version table ready (VARCHAR(255))")
    finally:
        await conn.close()


try:
    asyncio.run(_bootstrap())
except Exception as exc:
    print(
        f"WARNING: Could not bootstrap alembic_version: [{type(exc).__name__}] {exc}",
        file=sys.stderr,
    )

# Step 3: Write the fixed URLs to files for the shell
with open("/tmp/database_url.env", "w") as f:
    f.write(runtime_url)

with open("/tmp/database_admin_url.env", "w") as f:
    f.write(admin_url)
