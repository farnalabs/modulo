"""Org-scoped CRUD for ParameterSchema.

Deletion protection: delete_schema refuses if any Agent or ParameterSet
references this schema.
"""

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.base import PageResult, apply_updates
from modulo.db.crud.pagination import CursorPaginator
from modulo.db.models.agent import Agent
from modulo.db.models.parameter_schema import ParameterSchema
from modulo.db.models.parameter_set import ParameterSet


async def create_schema(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    name: str,
    description: str | None,
    parameters: list[dict[str, Any]],
    account_id: uuid.UUID,
) -> ParameterSchema:
    schema = ParameterSchema(
        organisation_id=org_id,
        name=name,
        description=description,
        parameters=parameters,
        account_id=account_id,
    )
    session.add(schema)
    await session.flush()
    return schema


async def get_schema(
    session: AsyncSession,
    schema_id: uuid.UUID,
) -> ParameterSchema | None:
    result = await session.execute(select(ParameterSchema).where(ParameterSchema.id == schema_id))
    return result.scalar_one_or_none()


async def list_schemas(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    cursor: str | None = None,
    limit: int = 20,
) -> PageResult[ParameterSchema]:
    q = select(ParameterSchema).where(ParameterSchema.organisation_id == org_id)
    total_result = await session.execute(
        select(func.count()).select_from(ParameterSchema).where(ParameterSchema.organisation_id == org_id)
    )
    total = total_result.scalar_one()

    if cursor is not None:
        paginator = CursorPaginator()
        cp = await paginator.paginate(
            session,
            q,
            cursor=cursor,
            limit=limit,
            model=ParameterSchema,
            compute_total=False,
        )
        return PageResult(
            items=cp.items,
            total=total,
            page=1,
            page_size=limit,
            next_cursor=cp.next_cursor,
            has_more=cp.has_more,
        )

    items = list((await session.execute(q.order_by(ParameterSchema.created_at.desc()).limit(limit + 1))).scalars())
    has_more = len(items) > limit
    items = items[:limit]
    return PageResult(items=items, total=total, page=1, page_size=limit, has_more=has_more)


async def update_schema(
    session: AsyncSession,
    schema_id: uuid.UUID,
    *,
    name: str | None = None,
    description: str | None = None,
    parameters: list[dict[str, Any]] | None = None,
    version: int,
) -> ParameterSchema | None:
    result = await session.execute(
        select(ParameterSchema)
        .where(
            ParameterSchema.id == schema_id,
            ParameterSchema.version == version,
        )
        .with_for_update()
    )
    schema = result.scalar_one_or_none()
    if schema is None:
        return None
    updates: dict[str, Any] = {}
    if name is not None:
        updates["name"] = name
    if description is not None:
        updates["description"] = description
    if parameters is not None:
        updates["parameters"] = parameters
    apply_updates(schema, updates)
    schema.version += 1
    await session.flush()
    return schema


async def delete_schema(
    session: AsyncSession,
    schema_id: uuid.UUID,
) -> bool:
    agent_count = (
        await session.execute(select(func.count()).select_from(Agent).where(Agent.parameter_schema_id == schema_id))
    ).scalar_one()
    if agent_count:
        return False

    set_count = (
        await session.execute(
            select(func.count()).select_from(ParameterSet).where(ParameterSet.parameter_schema_id == schema_id)
        )
    ).scalar_one()
    if set_count:
        return False

    result = await session.execute(select(ParameterSchema).where(ParameterSchema.id == schema_id))
    schema = result.scalar_one_or_none()
    if schema is None:
        return False
    await session.delete(schema)
    await session.flush()
    return True


async def get_schema_references(
    session: AsyncSession,
    schema_id: uuid.UUID,
) -> dict[str, list[uuid.UUID]]:
    refs: dict[str, list[uuid.UUID]] = {"agents": [], "sets": []}

    agent_results = await session.execute(select(Agent.id).where(Agent.parameter_schema_id == schema_id))
    refs["agents"] = list(agent_results.scalars().all())

    set_results = await session.execute(select(ParameterSet.id).where(ParameterSet.parameter_schema_id == schema_id))
    refs["sets"] = list(set_results.scalars().all())

    return refs
