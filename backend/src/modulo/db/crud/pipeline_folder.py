"""Org-scoped CRUD for PipelineFolder.

All functions assume the caller has set the RLS org context via set_rls_org()
before calling. The session must be within an active transaction.
"""

import uuid
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.base import apply_updates
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.pipeline_folder import PipelineFolder

_MAX_FOLDER_DEPTH = 8


async def _compute_folder_depth(session: AsyncSession, folder_id: uuid.UUID | None) -> int:
    """Compute the true nesting depth of a folder by walking the parent_id chain.

    Returns the actual ancestor count (0 for a top-level folder) so callers can
    enforce the ``_MAX_FOLDER_DEPTH`` cap. A pre-existing cycle in the data is
    bounded by a visited set so corrupted rows can never hang the walk.
    """
    depth = 0
    current_id = folder_id
    seen: set[uuid.UUID] = set()
    while current_id is not None:
        if current_id in seen:
            break
        seen.add(current_id)
        depth += 1
        result = await session.execute(select(PipelineFolder.parent_id).where(PipelineFolder.id == current_id))
        current_id = result.scalar_one_or_none()
    return depth


async def _assert_parent_exists(session: AsyncSession, parent_id: uuid.UUID) -> None:
    """Reject a parent_id that does not resolve to a folder in the caller's org.

    The SELECT runs under the RLS org context set by ``set_rls_org``, so it
    returns ``None`` both for a missing folder and for a folder owned by
    another organisation. Rejecting here keeps the folder tree tenant-scoped —
    a cross-org parent would otherwise be writable while its FK
    ``ondelete="CASCADE"`` runs as the table owner (bypassing RLS), creating a
    tenant-boundary data-loss path.

    Raises ``ValueError``; the route layer maps it to 422.
    """
    result = await session.execute(select(PipelineFolder.id).where(PipelineFolder.id == parent_id))
    if result.scalar_one_or_none() is None:
        raise ValueError(f"Parent folder not found: {parent_id}")


async def _assert_valid_parent(
    session: AsyncSession,
    folder_id: uuid.UUID,
    parent_id: uuid.UUID,
) -> None:
    """Reject an invalid parent assignment for a folder update.

    Enforces the folder-tree invariants that PRD §8.4 "Pipeline Folders"
    implies for an organisation-scoped nested folder tree:
    1. The parent must exist in the caller's organisation (a cross-org parent
       is rejected — referential actions bypass RLS).
    2. A folder cannot be its own parent.
    3. A folder cannot be moved under one of its own descendants (which would
       create an ancestry cycle).
    4. Nesting depth cannot exceed ``_MAX_FOLDER_DEPTH``.

    Raises ``ValueError`` on violation; the route layer maps it to 422.
    """
    if parent_id == folder_id:
        raise ValueError("A folder cannot be its own parent")
    await _assert_parent_exists(session, parent_id)
    depth = 0
    current_id: uuid.UUID | None = parent_id
    seen: set[uuid.UUID] = set()
    while current_id is not None:
        depth += 1
        if depth > _MAX_FOLDER_DEPTH:
            raise ValueError(f"Folder nesting depth would exceed {_MAX_FOLDER_DEPTH} levels")
        if current_id == folder_id:
            raise ValueError("Setting this parent would create a folder ancestry cycle")
        if current_id in seen:
            # A pre-existing cycle in the data — never silently extend it.
            raise ValueError("Setting this parent would create a folder ancestry cycle")
        seen.add(current_id)
        result = await session.execute(select(PipelineFolder.parent_id).where(PipelineFolder.id == current_id))
        current_id = result.scalar_one_or_none()


async def create_folder(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    name: str,
    account_id: uuid.UUID,
    parent_id: uuid.UUID | None = None,
) -> PipelineFolder:
    if parent_id is not None:
        await _assert_parent_exists(session, parent_id)
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
    if updates.get("parent_id") is not None:
        await _assert_valid_parent(session, folder_id, updates["parent_id"])
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
