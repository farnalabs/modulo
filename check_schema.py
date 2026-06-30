import os, asyncio, asyncpg

async def main():
    conn = await asyncpg.connect(os.environ['DATABASE_URL'])
    cols = await conn.fetch("SELECT column_name FROM information_schema.columns WHERE table_name = 'pipelines' ORDER BY ordinal_position")
    for c in cols:
        print(c['column_name'])
    await conn.close()

asyncio.run(main())
