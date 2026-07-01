"""Org-scoped CRUD for Node composite/tree operations.

All functions assume the caller has set the RLS org context via set_rls_org()
before calling. The session must be within an active transaction.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.models.node import Node


async def get_child_nodes(session: AsyncSession, parent_id: uuid.UUID) -> list[Node]:
    """Return all direct children of the given parent node."""
    result = await session.execute(
        select(Node).where(Node.parent_node_id == parent_id).order_by(Node.created_at)
    )
    return list(result.scalars().all())


async def set_parent_node(
    session: AsyncSession,
    node_id: uuid.UUID,
    parent_id: uuid.UUID | None,
) -> Node | None:
    """Set the parent of a node, returning the updated node or None if not found."""
    node = (
        await session.execute(select(Node).where(Node.id == node_id).with_for_update())
    ).scalar_one_or_none()
    if node is None:
        return None

    if parent_id is not None:
        parent = (
            await session.execute(select(Node).where(Node.id == parent_id).with_for_update())
        ).scalar_one_or_none()
        if parent is None:
            return None

    node.parent_node_id = parent_id
    await session.flush()
    return node
