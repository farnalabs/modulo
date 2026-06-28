#!/usr/bin/env python3
"""Delete stale Demo Corp orgs and re-run seed."""
import asyncio
import subprocess
import sys

from modulo.db.session import AsyncSessionLocal
from modulo.db.models import Organisation
from sqlalchemy import select


async def main():
    async with AsyncSessionLocal() as s:
        async with s.begin():
            result = await s.execute(select(Organisation).where(Organisation.slug == "demo-corp"))
            orgs = list(result.scalars().all())
            for o in orgs:
                print(f"Deleting org: {o.id} ({o.name})")
                await s.execute(Organisation.__table__.delete().where(Organisation.id == o.id))
        await s.commit()
    print("Stale orgs deleted. Now running seed...")
    sys.stdout.flush()
    result = subprocess.run([sys.executable, "/app/scripts/seed.py"], capture_output=True, text=True)
    print(result.stdout)
    if result.returncode != 0:
        print(result.stderr)


asyncio.run(main())
