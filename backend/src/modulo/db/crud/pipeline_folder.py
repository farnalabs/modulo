"""Org-scoped CRUD for PipelineFolder.

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
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.pipeline_folder import PipelineFolder


async def create_folder(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    name: str,
    account_id: uuid.UUID,
    parent_id: uuid.UUID | None = None,
) -> PipelineFolder:
    if parent_id is not None:
        await assert_parent_exists(session, PipelineFolder, parent_id)
        await check_parent_depth(session, PipelineFolder, parent_id)
    folder = PipelineFolder(
        organisation_id=org_id,
        name=name,
        account_id=account_id,
        parent_id=parent_id,
    )
    session.add(folder)
    await session.flush()
    return folder


async def list_folders(session: AsyncSession) -> list[PipelineFolder]:
    result = await session.execute(select(PipelineFolder).order_by(PipelineFolder.sort_order, PipelineFolder.name))
    return list(result.scalars().all())


async def get_folder(
    session: AsyncSession,
    folder_id: uuid.UUID,
) -> PipelineFolder | None:
    result = await session.execute(select(PipelineFolder).where(PipelineFolder.id == folder_id))
    return result.scalar_one_or_none()


async def update_folder(
    session: AsyncSession,
    folder_id: uuid.UUID,
    updates: dict[str, Any],
) -> PipelineFolder | None:
    folder = await get_folder(session, folder_id)
    if folder is None:
        return None
    if updates.get("parent_id") is not None:
        new_parent_id = updates["parent_id"]
        if new_parent_id == folder_id:
            raise ValueError("A folder cannot be its own parent")
        await assert_parent_exists(session, PipelineFolder, new_parent_id)
        if await folder_is_ancestor(session, PipelineFolder, new_parent_id, folder_id):
            raise ValueError("A folder cannot be moved under one of its own descendants")
        await check_parent_depth(session, PipelineFolder, new_parent_id)
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

    # SET NULL on pipelines in this folder
    await session.execute(update(Pipeline).where(Pipeline.folder_id == folder_id).values(folder_id=None))

    # SET NULL on children's parent_id
    await session.execute(update(PipelineFolder).where(PipelineFolder.parent_id == folder_id).values(parent_id=None))

    await session.delete(folder)
    await session.flush()
    return True


async def move_pipeline_to_folder(
    session: AsyncSession,
    pipeline_id: uuid.UUID,
    folder_id: uuid.UUID | None,
) -> Pipeline | None:
    result = await session.execute(select(Pipeline).where(Pipeline.id == pipeline_id))
    pipeline = result.scalar_one_or_none()
    if pipeline is None:
        return None
    if folder_id is not None:
        folder = await get_folder(session, folder_id)
        if folder is None:
            raise ValueError(f"Folder not found: {folder_id}")
    pipeline.folder_id = folder_id
    await session.flush()
    return pipeline
