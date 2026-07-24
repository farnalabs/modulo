"""Org-scoped CRUD for SavedView.

All functions assume the caller has set the RLS org context via set_rls_org()
before calling. The session must be within an active transaction.
"""

import uuid
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.base import PageResult, apply_updates
from modulo.db.models.view import SavedView


async def create_view(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    name: str,
    view_type: str,
    account_id: uuid.UUID,
    description: str | None = None,
    filters: dict[str, Any] | None = None,
    columns: list[str] | None = None,
    sort_by: str | None = None,
    sort_order: str = "desc",
) -> SavedView:
    view = SavedView(
        organisation_id=org_id,
        name=name,
        view_type=view_type,
        account_id=account_id,
        description=description,
        filters=filters or {},
        columns=columns,
        sort_by=sort_by,
        sort_order=sort_order,
    )
    session.add(view)
    await session.flush()
    return view


async def get_view(session: AsyncSession, view_id: uuid.UUID, *, include_deleted: bool = False) -> SavedView | None:
    stmt = select(SavedView).where(SavedView.id == view_id)
    if not include_deleted:
        stmt = stmt.where(SavedView.deleted_at.is_(None))
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def list_views(
    session: AsyncSession,
    *,
    view_type: str | None = None,
    page: int = 1,
    page_size: int = 20,
    include_deleted: bool = False,
) -> PageResult[SavedView]:
    conditions = []
    if view_type is not None:
        conditions.append(SavedView.view_type == view_type)
    if not include_deleted:
        conditions.append(SavedView.deleted_at.is_(None))

    count_q = select(func.count()).select_from(SavedView).where(*conditions)
    try:
        total = (await session.execute(count_q)).scalar_one()
    except ProgrammingError:
        return PageResult(items=[], total=0, page=page, page_size=page_size)

    offset = (page - 1) * page_size
    items = list(
        (
            await session.execute(
                select(SavedView)
                .where(*conditions)
                .order_by(SavedView.created_at.desc())
                .offset(offset)
                .limit(page_size)
            )
        ).scalars()
    )
    return PageResult(items=items, total=total, page=page, page_size=page_size)


async def update_view(
    session: AsyncSession,
    view_id: uuid.UUID,
    updates: dict[str, Any],
) -> SavedView | None:
    view = await get_view(session, view_id)
    if view is None:
        return None
    apply_updates(view, updates)
    await session.flush()
    return view


async def soft_delete_view(session: AsyncSession, view_id: uuid.UUID) -> SavedView | None:
    """Soft-delete: set deleted_at. Returns None if not found or already deleted."""
    result = await session.execute(
        update(SavedView)
        .where(SavedView.id == view_id, SavedView.deleted_at.is_(None))
        .values(deleted_at=func.now())
        .returning(SavedView)
    )
    await session.flush()
    return result.scalar_one_or_none()


async def restore_view(session: AsyncSession, view_id: uuid.UUID) -> SavedView | None:
    """Restore a soft-deleted view. Returns None if not found."""
    result = await session.execute(
        update(SavedView)
        .where(SavedView.id == view_id, SavedView.deleted_at.is_not(None))
        .values(deleted_at=None)
        .returning(SavedView)
    )
    await session.flush()
    return result.scalar_one_or_none()


async def delete_view(session: AsyncSession, view_id: uuid.UUID) -> bool:
    """Hard-delete. Only call from admin cleanup."""
    view = await get_view(session, view_id, include_deleted=True)
    if view is None:
        return False
    await session.delete(view)
    await session.flush()
    return True
