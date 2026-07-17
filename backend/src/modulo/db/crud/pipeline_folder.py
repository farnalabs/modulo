"""Org-scoped CRUD for PipelineFolder.

All functions assume the caller has set the RLS org context via set_rls_org()
before calling. The session must be within an active transaction.
"""

import logging
import uuid
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.base import apply_updates
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.pipeline_folder import PipelineFolder

_log = logging.getLogger(__name__)

_MAX_FOLDER_DEPTH = 8


async def _compute_folder_depth(session: AsyncSession, folder_id: uuid.UUID | None) -> int:
    """Compute the nesting depth of a folder by walking parent_id chain."""
    depth = 0
    current_id = folder_id
    while current_id is not None:
        depth += 1
        if depth >= _MAX_FOLDER_DEPTH:
            return depth
        result = await session.execute(select(PipelineFolder.parent_id).where(PipelineFolder.id == current_id))
        current_id = result.scalar_one_or_none()
    return depth


async def create_folder(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    name: str,
    account_id: uuid.UUID,
    parent_id: uuid.UUID | None = None,
) -> PipelineFolder:
    if parent_id is not None:
        depth = await _compute_folder_depth(session, parent_id)
        if depth > _MAX_FOLDER_DEPTH:
            raise ValueError(f"Folder nesting depth would exceed {_MAX_FOLDER_DEPTH} levels")
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
