import asyncio
import asyncpg
import os
url = os.environ.get("DATABASE_URL", "")
url = url.replace("postgresql+asyncpg://", "postgres://").split("?")[0]
print(f"URL: {url[:60]}...")

async def main():
    conn = await asyncpg.connect(url)
    try:
        r = await conn.fetch("SELECT COUNT(*) as cnt FROM users")
        print(f"Users: {r[0]['cnt']}")
        if r[0]['cnt'] > 0:
            rows = await conn.fetch("SELECT email, org_role FROM users ORDER BY email")
            for row in rows:
                print(f"  {row['email']} ({row['org_role']})")
    except Exception as e:
        print(f"Error: {e}")
    finally:
        await conn.close()

asyncio.run(main())
