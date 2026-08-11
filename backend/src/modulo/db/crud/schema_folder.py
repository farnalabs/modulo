"""Org-scoped CRUD for SchemaFolder.

All functions assume the caller has set the RLS org context via set_rls_org()
before calling. The session must be within an active transaction.
"""

import uuid
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.base import apply_updates
from modulo.db.models.schema import Schema, SchemaFolder

_MAX_FOLDER_DEPTH = 8


async def _compute_folder_depth(session: AsyncSession, folder_id: uuid.UUID | None) -> int:
    """Compute the nesting depth of a folder by walking parent_id chain."""
    depth = 0
    current_id = folder_id
    while current_id is not None:
        depth += 1
        if depth >= _MAX_FOLDER_DEPTH:
            return depth
        result = await session.execute(select(SchemaFolder.parent_id).where(SchemaFolder.id == current_id))
        current_id = result.scalar_one_or_none()
    return depth


async def _folder_is_ancestor(
    session: AsyncSession,
    folder_id: uuid.UUID,
    ancestor_id: uuid.UUID,
) -> bool:
    """Return True if ancestor_id appears in folder_id's parent chain (or equals it)."""
    current_id: uuid.UUID | None = folder_id
    steps = 0
    while current_id is not None and steps <= _MAX_FOLDER_DEPTH:
        if current_id == ancestor_id:
            return True
        result = await session.execute(select(SchemaFolder.parent_id).where(SchemaFolder.id == current_id))
        current_id = result.scalar_one_or_none()
        steps += 1
    return False


async def _check_parent_depth(session: AsyncSession, parent_id: uuid.UUID) -> None:
    """Reject a parent whose chain would place a child beyond _MAX_FOLDER_DEPTH levels."""
    depth = await _compute_folder_depth(session, parent_id)
    if depth >= _MAX_FOLDER_DEPTH:
        raise ValueError(f"Folder nesting depth would exceed {_MAX_FOLDER_DEPTH} levels")


async def _parent_exists_in_org(session: AsyncSession, parent_id: uuid.UUID) -> bool:
    """Return True if a folder with ``parent_id`` is visible in the current RLS org.

    FK checks run as the table owner and bypass RLS, so a caller who knows
    another org's folder UUID could otherwise attach a folder under a hidden
    parent. This explicit org-scoped existence check closes that hole.
    """
    result = await session.execute(select(SchemaFolder.id).where(SchemaFolder.id == parent_id))
    return result.scalar_one_or_none() is not None


async def create_folder(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    name: str,
    account_id: uuid.UUID,
    parent_id: uuid.UUID | None = None,
) -> SchemaFolder:
    if parent_id is not None:
        if not await _parent_exists_in_org(session, parent_id):
            raise ValueError(f"Parent folder not found: {parent_id}")
        await _check_parent_depth(session, parent_id)
    folder = SchemaFolder(
        organisation_id=org_id,
        name=name,
        account_id=account_id,
        parent_id=parent_id,
    )
    session.add(folder)
    await session.flush()
    return folder


async def list_folders(session: AsyncSession) -> list[SchemaFolder]:
    result = await session.execute(select(SchemaFolder).order_by(SchemaFolder.sort_order, SchemaFolder.name))
    return list(result.scalars().all())


async def get_folder(
    session: AsyncSession,
    folder_id: uuid.UUID,
) -> SchemaFolder | None:
    result = await session.execute(select(SchemaFolder).where(SchemaFolder.id == folder_id))
    return result.scalar_one_or_none()


async def update_folder(
    session: AsyncSession,
    folder_id: uuid.UUID,
    updates: dict[str, Any],
) -> SchemaFolder | None:
    folder = await get_folder(session, folder_id)
    if folder is None:
        return None
    if "parent_id" in updates:
        new_parent_id = updates["parent_id"]
        if new_parent_id is not None:
            if new_parent_id == folder_id:
                raise ValueError("A folder cannot be its own parent")
            if not await _parent_exists_in_org(session, new_parent_id):
                raise ValueError(f"Parent folder not found: {new_parent_id}")
            if await _folder_is_ancestor(session, new_parent_id, folder_id):
                raise ValueError("A folder cannot be moved under one of its own descendants")
            await _check_parent_depth(session, new_parent_id)
    apply_updates(folder, updates)
    await session.flush()
    return folder


async def delete_folder(
    session: AsyncSession,
    folder_id: uuid.UUID,
) -> bool:
    folder = await get_folder(session, folder_id)
    if folder is None:
        return False

    # SET NULL on schemas in this folder
    await session.execute(update(Schema).where(Schema.folder_id == folder_id).values(folder_id=None))

    # SET NULL on children's parent_id
    await session.execute(update(SchemaFolder).where(SchemaFolder.parent_id == folder_id).values(parent_id=None))

    await session.delete(folder)
    await session.flush()
    return True


async def move_schema_to_folder(
    session: AsyncSession,
    schema_id: uuid.UUID,
    folder_id: uuid.UUID | None,
) -> Schema | None:
    result = await session.execute(select(Schema).where(Schema.id == schema_id))
    schema = result.scalar_one_or_none()
    if schema is None:
        return None
    if folder_id is not None:
        folder = await get_folder(session, folder_id)
        if folder is None:
            raise ValueError(f"Folder not found: {folder_id}")
    schema.folder_id = folder_id
    await session.flush()
    return schema
