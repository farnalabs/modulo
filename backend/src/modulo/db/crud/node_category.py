"""Org-scoped CRUD for NodeCategory.

All functions assume the caller has set the RLS org context via set_rls_org()
before calling. The session must be within an active transaction.
"""

import uuid
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import ProgrammingError
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
        account_id=account_id,
        description=description,
        color=color,
        icon=icon,
        sort_order=sort_order,
    )
    session.add(category)
    await session.flush()
    return category


async def get_node_category(
    session: AsyncSession, category_id: uuid.UUID, *, org_id: uuid.UUID, include_deleted: bool = False
) -> NodeCategory | None:
    stmt = select(NodeCategory).where(
        NodeCategory.id == category_id,
        NodeCategory.organisation_id == org_id,
    )
    if not include_deleted:
        stmt = stmt.where(NodeCategory.deleted_at.is_(None))
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def list_node_categories(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    page: int = 1,
    page_size: int = 20,
    include_deleted: bool = False,
) -> PageResult[NodeCategory]:
    offset = (page - 1) * page_size
    where_conditions = [NodeCategory.organisation_id == org_id]
    if not include_deleted:
        where_conditions.append(NodeCategory.deleted_at.is_(None))
    try:
        total = (
            await session.execute(select(func.count()).select_from(NodeCategory).where(*where_conditions))
        ).scalar_one()
    except ProgrammingError:
        return PageResult(items=[], total=0, page=page, page_size=page_size)
    stmt = (
        select(NodeCategory)
        .where(*where_conditions)
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
    *,
    org_id: uuid.UUID,
) -> NodeCategory | None:
    category = await get_node_category(session, category_id, org_id=org_id)
    if category is None:
        return None
    apply_updates(category, updates)
    await session.flush()
    return category


async def soft_delete_node_category(
    session: AsyncSession, category_id: uuid.UUID, *, org_id: uuid.UUID
) -> NodeCategory | None:
    """Soft-delete: set deleted_at. Returns None if not found or already deleted."""
    result = await session.execute(
        update(NodeCategory)
        .where(
            NodeCategory.id == category_id,
            NodeCategory.organisation_id == org_id,
            NodeCategory.deleted_at.is_(None),
        )
        .values(deleted_at=func.now())
        .returning(NodeCategory)
    )
    await session.flush()
    return result.scalar_one_or_none()


async def restore_node_category(
    session: AsyncSession, category_id: uuid.UUID, *, org_id: uuid.UUID
) -> NodeCategory | None:
    """Restore a soft-deleted node category. Returns None if not found."""
    result = await session.execute(
        update(NodeCategory)
        .where(
            NodeCategory.id == category_id,
            NodeCategory.organisation_id == org_id,
            NodeCategory.deleted_at.is_not(None),
        )
        .values(deleted_at=None)
        .returning(NodeCategory)
    )
    await session.flush()
    return result.scalar_one_or_none()


async def delete_node_category(session: AsyncSession, category_id: uuid.UUID, *, org_id: uuid.UUID) -> bool:
    """Hard-delete. Only call from admin cleanup."""
    category = await get_node_category(session, category_id, org_id=org_id, include_deleted=True)
    if category is None:
        return False
    await session.delete(category)
    await session.flush()
    return True
