"""Org-scoped CRUD for Schema and SchemaVersion.

Deletion protection: delete_schema refuses if any Agent, PipelineSnapshot,
or LibraryPrimitive references this schema. Use force=True to skip all checks.
All functions require RLS org context to be set by the caller.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import cast, func, or_, select, String
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.base import PageResult, apply_updates
from modulo.db.models.agent import Agent
from modulo.db.models.library_primitive import LibraryPrimitive
from modulo.db.models.pipeline_snapshot import PipelineSnapshot
from modulo.db.models.schema import Schema, SchemaVersion


class SchemaDeletionProtectedError(Exception):
    """Raised when a Schema cannot be deleted because references exist."""

    def __init__(self, schema_id: uuid.UUID, detail: str | None = None) -> None:
        if detail is None:
            detail = (
                f"Schema {schema_id} cannot be deleted: one or more Agents reference it. "
                "Reassign or delete those Agents first."
            )
        super().__init__(detail)
        self.schema_id = schema_id


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


async def create_schema(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    name: str,
    account_id: uuid.UUID,
    description: str | None = None,
    abstract_name: str | None = None,
) -> Schema:
    schema = Schema(
        organisation_id=org_id,
        name=name,
        account_id=account_id,
        description=description,
        abstract_name=abstract_name,
    )
    session.add(schema)
    await session.flush()
    return schema


async def get_schema(session: AsyncSession, schema_id: uuid.UUID) -> Schema | None:
    result = await session.execute(select(Schema).where(Schema.id == schema_id))
    return result.scalar_one_or_none()


async def list_schemas(
    session: AsyncSession,
    *,
    page: int = 1,
    page_size: int = 20,
) -> PageResult[Schema]:
    offset = (page - 1) * page_size
    total = (await session.execute(select(func.count()).select_from(Schema))).scalar_one()
    items = list(
        (
            await session.execute(select(Schema).order_by(Schema.created_at.desc()).offset(offset).limit(page_size))
        ).scalars()
    )
    return PageResult(items=items, total=total, page=page, page_size=page_size)


async def update_schema(
    session: AsyncSession,
    schema_id: uuid.UUID,
    updates: dict[str, Any],
) -> Schema | None:
    schema = await get_schema(session, schema_id)
    if schema is None:
        return None
    apply_updates(schema, updates)
    await session.flush()
    return schema


async def deprecate_schema(session: AsyncSession, schema_id: uuid.UUID) -> Schema | None:
    """Mark a schema as deprecated."""
    schema = await get_schema(session, schema_id)
    if schema is None:
        return None
    schema.deprecated = True
    schema.deprecated_at = datetime.now(UTC)
    await session.flush()
    return schema


async def delete_schema(
    session: AsyncSession,
    schema_id: uuid.UUID,
    force: bool = False,
) -> bool:
    """Delete a schema.

    Raises SchemaDeletionProtectedError if references exist (Agents,
    PipelineSnapshots, LibraryPrimitives) unless force=True.
    """
    if not force:
        refs: list[str] = []

        agent_count = (
            await session.execute(
                select(func.count())
                .select_from(Agent)
                .where(
                    or_(
                        Agent.input_schema_id == schema_id,
                        Agent.output_schema_id == schema_id,
                    )
                )
            )
        ).scalar_one()
        if agent_count:
            refs.append("Agents")

        pipelines_with_ref = (
            await session.execute(
                select(func.count())
                .select_from(PipelineSnapshot)
                .where(
                    cast(PipelineSnapshot.schema_pins_json, String).contains(
                        str(schema_id)
                    )
                )
            )
        ).scalar_one()
        if pipelines_with_ref:
            refs.append("PipelineSnapshots (schema_pins_json)")

        library_refs = (
            await session.execute(
                select(func.count())
                .select_from(LibraryPrimitive)
                .where(
                    LibraryPrimitive.primitive_type == "schema",
                    LibraryPrimitive.source == "local",
                    cast(LibraryPrimitive.content_json, String).contains(
                        str(schema_id)
                    ),
                )
            )
        ).scalar_one()
        if library_refs:
            refs.append("LibraryPrimitives (local schema entries)")

        if refs:
            detail = (
                f"Schema {schema_id} cannot be deleted: references exist in "
                f"{' and '.join(refs)}. "
                "Remove those references or use force=true to delete unconditionally."
            )
            raise SchemaDeletionProtectedError(schema_id, detail=detail)

    schema = await get_schema(session, schema_id)
    if schema is None:
        return False
    await session.delete(schema)
    await session.flush()
    return True


# ---------------------------------------------------------------------------
# SchemaVersion
# ---------------------------------------------------------------------------


async def create_schema_version(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    schema_id: uuid.UUID,
    version: str,
    version_number: int,
    definition_json: dict[str, Any],
    account_id: uuid.UUID,
    published: bool = False,
) -> SchemaVersion:
    sv = SchemaVersion(
        organisation_id=org_id,
        schema_id=schema_id,
        version=version,
        version_number=version_number,
        definition_json=definition_json,
        account_id=account_id,
        published=published,
    )
    session.add(sv)
    await session.flush()
    return sv


async def get_schema_version(
    session: AsyncSession,
    schema_id: uuid.UUID,
    version: str,
) -> SchemaVersion | None:
    result = await session.execute(
        select(SchemaVersion).where(
            SchemaVersion.schema_id == schema_id,
            SchemaVersion.version == version,
        )
    )
    return result.scalar_one_or_none()


async def list_schema_versions(
    session: AsyncSession,
    schema_id: uuid.UUID,
    *,
    page: int = 1,
    page_size: int = 20,
) -> PageResult[SchemaVersion]:
    offset = (page - 1) * page_size
    total = (
        await session.execute(
            select(func.count()).select_from(SchemaVersion).where(SchemaVersion.schema_id == schema_id)
        )
    ).scalar_one()
    items = list(
        (
            await session.execute(
                select(SchemaVersion)
                .where(SchemaVersion.schema_id == schema_id)
                .order_by(SchemaVersion.version_number.desc())
                .offset(offset)
                .limit(page_size)
            )
        ).scalars()
    )
    return PageResult(items=items, total=total, page=page, page_size=page_size)
