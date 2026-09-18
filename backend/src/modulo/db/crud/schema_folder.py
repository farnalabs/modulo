"""Org-scoped CRUD for SchemaFolder.

All functions assume the caller has set the RLS org context via set_rls_org()
before calling. The session must be within an active transaction.
"""

import uuid
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.base import apply_updates
from modulo.db.crud.folder_tree import (
    assert_parent_exists,
    check_parent_depth,
    folder_is_ancestor,
)
from modulo.db.models.schema import Schema, SchemaFolder


async def create_folder(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    name: str,
    account_id: uuid.UUID,
    parent_id: uuid.UUID | None = None,
) -> SchemaFolder:
    if parent_id is not None:
        await assert_parent_exists(session, SchemaFolder, parent_id)
        await check_parent_depth(session, SchemaFolder, parent_id)
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
            await assert_parent_exists(session, SchemaFolder, new_parent_id)
            if await folder_is_ancestor(session, SchemaFolder, new_parent_id, folder_id):
                raise ValueError("A folder cannot be moved under one of its own descendants")
            await check_parent_depth(session, SchemaFolder, new_parent_id)
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
    organisation_id: uuid.UUID | None = None,
) -> Schema | None:
    result = await session.execute(select(Schema).where(Schema.id == schema_id))
    schema = result.scalar_one_or_none()
    if schema is None:
        return None
    if folder_id is not None:
        folder = await get_folder(session, folder_id)
        if folder is None:
            raise ValueError(f"Folder not found: {folder_id}")
        # On non-Postgres backends RLS does not filter the folder read, so a
        # caller could otherwise attach an owned schema into another org's
        # folder. Assert ownership explicitly (the Postgres path already
        # collapses a cross-org folder to None via RLS → same 422 below).
        if organisation_id is not None and folder.organisation_id != organisation_id:
            raise ValueError(f"Folder not found: {folder_id}")
    schema.folder_id = folder_id
    await session.flush()
    return schema
