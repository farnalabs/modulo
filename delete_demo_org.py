#!/usr/bin/env python3
"""Delete the stale Demo Corp org so seed can run fresh."""
import asyncio
from modulo.db.session import AsyncSessionLocal
from modulo.db.models import Organisation
from sqlalchemy import select


async def main():
    async with AsyncSessionLocal() as s:
        async with s.begin():
            o = (await s.execute(select(Organisation).where(Organisation.slug == "demo-corp"))).scalar_one_or_none()
            if o:
                print(f"Found org: {o.id}, deleting...")
                await s.execute(Organisation.__table__.delete().where(Organisation.slug == "demo-corp"))
                await s.commit()
                print("Deleted")
            else:
                print("No stale org found")


asyncio.run(main())
