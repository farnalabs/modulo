"""Seed 22 library schema definitions into the database.

Usage:
    uv run python -m scripts.seed_library_schemas
"""

import asyncio
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from modulo.core.seed_data.library_schemas import SCHEMAS
from modulo.db.crud.schema import create_schema, create_schema_version
from modulo.db.models.schema import Schema
from modulo.settings import get_settings

ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
ACCOUNT_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")


async def seed() -> None:
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    async with AsyncSession(engine) as session:
        for entry in SCHEMAS:
            existing = (
                await session.execute(
                    select(Schema).where(
                        Schema.organisation_id == ORG_ID,
                        Schema.name == entry["name"],
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                print(f"  SKIP  {entry['name']} — already exists")
                continue

            schema = await create_schema(
                session,
                org_id=ORG_ID,
                name=entry["name"],
                account_id=ACCOUNT_ID,
                description=entry["description"],
            )
            await create_schema_version(
                session,
                org_id=ORG_ID,
                schema_id=schema.id,
                version="1.0",
                version_number=1,
                definition_json=entry["definition"],
                account_id=ACCOUNT_ID,
                published=True,
            )
            print(f"  CREATED  {entry['name']}")

        await session.commit()
    await engine.dispose()
    print(f"\nSeeded {len(SCHEMAS)} library schema definitions.")


if __name__ == "__main__":
    asyncio.run(seed())
