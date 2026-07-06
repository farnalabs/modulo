"""Org-scoped CRUD for CompositeTemplate.

All functions assume the caller has set the RLS org context via set_rls_org()
before calling. The session must be within an active transaction.
"""

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.base import PageResult, apply_updates
from modulo.db.models.composite_template import CompositeTemplate


async def create_composite_template(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    account_id: uuid.UUID,
    name: str,
    sub_pipeline_graph_json: dict[str, Any],
    parameter_ports_json: list[dict[str, Any]],
    description: str | None = None,
    input_schema_id: uuid.UUID | None = None,
    output_schema_id: uuid.UUID | None = None,
    version: str = "1.0.0",
) -> CompositeTemplate:
    template = CompositeTemplate(
        organisation_id=org_id,
        account_id=account_id,
        name=name,
        description=description,
        sub_pipeline_graph_json=sub_pipeline_graph_json,
        parameter_ports_json=parameter_ports_json,
        input_schema_id=input_schema_id,
        output_schema_id=output_schema_id,
        version=version,
    )
    session.add(template)
    await session.flush()
    return template


async def get_composite_template(
    session: AsyncSession,
    template_id: uuid.UUID,
) -> CompositeTemplate | None:
    result = await session.execute(
        select(CompositeTemplate).where(CompositeTemplate.id == template_id)
    )
    return result.scalar_one_or_none()


async def list_composite_templates(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    page: int = 1,
    page_size: int = 20,
) -> PageResult[CompositeTemplate]:
    offset = (page - 1) * page_size
    try:
        total = (
            await session.execute(
                select(func.count()).select_from(CompositeTemplate).where(
                    CompositeTemplate.organisation_id == org_id
                )
            )
        ).scalar_one()
    except ProgrammingError:
        return PageResult(items=[], total=0, page=page, page_size=page_size)
    stmt = (
        select(CompositeTemplate)
        .where(CompositeTemplate.organisation_id == org_id)
        .order_by(CompositeTemplate.name)
        .offset(offset)
        .limit(page_size)
    )
    result = await session.execute(stmt)
    items = list(result.scalars().all())
    return PageResult(items=items, total=total, page=page, page_size=page_size)


async def update_composite_template(
    session: AsyncSession,
    template_id: uuid.UUID,
    updates: dict[str, Any],
) -> CompositeTemplate | None:
    template = await get_composite_template(session, template_id)
    if template is None:
        return None
    apply_updates(template, updates)
    await session.flush()
    return template


async def delete_composite_template(
    session: AsyncSession,
    template_id: uuid.UUID,
) -> bool:
    template = await get_composite_template(session, template_id)
    if template is None:
        return False
    await session.delete(template)
    await session.flush()
    return True
