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


async def seed_bundled_runner_profile(session: AsyncSession, org_id: uuid.UUID, account_id: uuid.UUID) -> None:
    """Seed the per-org "Bundled Runner (Docker)" EnvironmentProfile (FAR-590 D4).

    Shipped-template seeding at org-creation (owned by the org's account) —
    idempotent: an org that already carries a live Bundled Runner profile row
    keeps it (operator-pinned older digests survive; template updates are
    surfaced live by the drift helper, never silently applied).
    """
    from sqlalchemy import text

    from modulo.db.bundled_runner_template import (
        TEMPLATE_PROFILE_NAME,
        build_bundled_runner_profile_values,
    )
    from modulo.db.models.environment_profile import EnvironmentProfile

    existing = await session.execute(
        text(
            "SELECT id FROM environment_profiles "
            "WHERE organisation_id = :oid AND provider_type = 'runner_docker' "
            "AND deleted_at IS NULL LIMIT 1"
        ),
        {"oid": str(org_id)},
    )
    if existing.fetchone() is not None:
        return
    values = build_bundled_runner_profile_values()
    profile = EnvironmentProfile(
        organisation_id=org_id,
        account_id=account_id,
        name=values["name"],
        description=values["description"],
        provider_type=values["provider_type"],
        image_ref=values["image_ref"],
        capabilities_json=values["capabilities_json"],
        config_json=values["config_json"],
        network_policy=values["network_policy"],
        initialisation_strategy=values["initialisation_strategy"],
        secret_refs_json=values["secret_refs_json"],
        persistence_policy=values["persistence_policy"],
        visibility="org",
    )
    session.add(profile)
    await session.flush()
    _log.info(
        "seed.bundled_runner_profile_created",
        extra={"org_id": str(org_id), "profile_name": TEMPLATE_PROFILE_NAME},
    )
