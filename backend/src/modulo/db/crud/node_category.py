"""Org-scoped CRUD for NodeCategory.

All functions assume the caller has set the RLS org context via set_rls_org()
before calling. The session must be within an active transaction.
"""

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.base import PageResult, apply_updates
from modulo.db.models.node_category import NodeCategory


async def create_node_category(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    name: str,
    account_id: uuid.UUID,
    description: str | None = None,
    color: str = "#6366f1",
    icon: str | None = None,
    sort_order: int = 0,
) -> NodeCategory:
    category = NodeCategory(
        organisation_id=org_id,
        name=name,
        created_by=account_id,
        description=description,
        color=color,
        icon=icon,
        sort_order=sort_order,
    )
    session.add(category)
    await session.flush()
    return category


async def get_node_category(session: AsyncSession, category_id: uuid.UUID) -> NodeCategory | None:
    result = await session.execute(select(NodeCategory).where(NodeCategory.id == category_id))
    return result.scalar_one_or_none()


async def list_node_categories(
    session: AsyncSession,
    *,
    page: int = 1,
    page_size: int = 20,
) -> PageResult[NodeCategory]:
    offset = (page - 1) * page_size
    total = (await session.execute(select(func.count()).select_from(NodeCategory))).scalar_one()
    stmt = (
        select(NodeCategory)
        .order_by(NodeCategory.sort_order, NodeCategory.name)
        .offset(offset)
        .limit(page_size)
    )
    items = list((await session.execute(stmt)).scalars())
    return PageResult(items=items, total=total, page=page, page_size=page_size)


async def update_node_category(
    session: AsyncSession,
    category_id: uuid.UUID,
    updates: dict[str, Any],
) -> NodeCategory | None:
    category = await get_node_category(session, category_id)
    if category is None:
        return None
    apply_updates(category, updates)
    await session.flush()
    return category


async def delete_node_category(session: AsyncSession, category_id: uuid.UUID) -> bool:
    category = await get_node_category(session, category_id)
    if category is None:
        return False
    await session.delete(category)
    await session.flush()
    return True
