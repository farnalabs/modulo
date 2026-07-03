"""Seed 3 pipeline template definitions into the database.

Usage:
    uv run python -m scripts.seed_library_templates
"""

import asyncio
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from modulo.core.library_service import _MODULO_PRIMITIVES
from modulo.db.crud.library_primitive import create_library_primitive
from modulo.db.models.library_primitive import LibraryPrimitive
from modulo.settings import get_settings

ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
ACCOUNT_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")


async def seed() -> None:
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    async with AsyncSession(engine) as session:
        templates = [p for p in _MODULO_PRIMITIVES if p.primitive_type == "pipeline_template"]
        for template in templates:
            existing = (
                await session.execute(
                    select(LibraryPrimitive).where(
                        LibraryPrimitive.organisation_id == ORG_ID,
                        LibraryPrimitive.name == template.name,
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                print(f"  SKIP  {template.name} — already exists")
                continue

            await create_library_primitive(
                session,
                org_id=ORG_ID,
                source="local",
                primitive_type="pipeline_template",
                name=template.name,
                slug=template.slug,
                description=template.description,
                author="modulo",
                version="1.0",
                tags=list(template.tags) if template.tags else [],
                content_json=dict(template.content_json),
                source_url=None,
                forked_from=template.id,
                checksum=None,
                ed25519_signature=None,
                verified=None,
                download_count=None,
                average_rating=None,
                review_count=None,
                owner_team_id=None,
                visibility="org",
                account_id=ACCOUNT_ID,
            )
            print(f"  CREATED  {template.name}")

        await session.commit()
    await engine.dispose()
    print(f"\nSeeded {len(templates)} pipeline template definitions.")


if __name__ == "__main__":
    asyncio.run(seed())
