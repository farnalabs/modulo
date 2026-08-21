"""System schema seeding on organisation creation and startup."""

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.models.schema import Schema, SchemaVersion

_log = logging.getLogger(__name__)

SYSTEM_SCHEMAS = [
    {
        "abstract_name": "_system.schema_freeform",
        "name": "schema_freeform",
        "version": "v1",
        "definition": {"type": "object"},
    },
    {
        "abstract_name": "_system.schema_text",
        "name": "schema_text",
        "version": "v1",
        "definition": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
    {
        "abstract_name": "_system.schema_trigger_payload",
        "name": "schema_trigger_payload",
        "version": "v1",
        "definition": {
            "type": "object",
            "properties": {},
            "additionalProperties": True,
        },
    },
]


async def seed_system_schemas(session: AsyncSession, org_id: uuid.UUID, account_id: uuid.UUID) -> None:
    """Create system schemas for an organisation if they don't already exist."""
    for spec in SYSTEM_SCHEMAS:
        existing = await session.execute(
            select(Schema).where(
                Schema.organisation_id == org_id,
                Schema.name == spec["name"],
            )
        )
        if existing.scalar_one_or_none() is not None:
            continue

        schema = Schema(
            organisation_id=org_id,
            name=spec["name"],
            abstract_name=spec["abstract_name"],
            account_id=account_id,
            description=f"System schema: {spec['name']}",
            system=True,
        )
        session.add(schema)
        await session.flush()

        schema_version = SchemaVersion(
            organisation_id=org_id,
            schema_id=schema.id,
            version=spec["version"],
            version_number=1,
            definition_json=spec["definition"],
            account_id=account_id,
            published=True,
        )
        session.add(schema_version)
        _log.info("seed.system_schema_created", extra={"org_id": str(org_id), "schema_name": spec["name"]})
