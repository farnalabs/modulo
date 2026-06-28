"""Bootstrap the Fly.io Postgres database — create alembic_version with
VARCHAR(255) and run migrations. Run via: fly ssh console -C ".venv/bin/python3 /app/deploy/fly/bootstrap.py"
"""
import asyncio
import asyncpg
import os

async def main():
    url = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgres://").replace("?sslmode=disable", "")
    print(f"Connecting to: {url[:60]}...")
    conn = await asyncpg.connect(url, ssl=False)
    await conn.execute("CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(255) NOT NULL PRIMARY KEY)")
    print("Created alembic_version table")
    await conn.close()

asyncio.run(main())
