"""Run at container startup to prepare the database.
1. Fix DATABASE_URL for SQLAlchemy async driver
2. Create alembic_version table with VARCHAR(255) for branch migrations
"""
import os
import sys

# Step 1: Fix DATABASE_URL and persist it for child processes
url = os.environ.get("DATABASE_URL", "")
original = url
url = url.replace("postgres://", "postgresql+asyncpg://", 1)
url = url.replace("?sslmode=disable", "?ssl=disable")
url = url.replace("&sslmode=disable", "")
os.environ["DATABASE_URL"] = url
if url != original:
    print(f"Fixed DATABASE_URL scheme + stripped sslmode")

# Step 2: Create alembic_version table with VARCHAR(255)
# Branch migration IDs exceed the default VARCHAR(32).
import asyncio
import asyncpg

async def _bootstrap():
    pg_url = url.replace("postgresql+asyncpg://", "postgres://")
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
    print(f"WARNING: Could not bootstrap alembic_version: [{type(exc).__name__}] {exc}", file=sys.stderr)

# Step 3: Write the fixed URL to a file so the shell can read it
with open("/tmp/database_url.env", "w") as f:
    f.write(url)
