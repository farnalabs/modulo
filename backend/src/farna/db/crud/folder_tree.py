"""Shared org-scoped folder-tree validation.

``PipelineFolder`` and ``SchemaFolder`` are structurally identical
self-referencing folder trees (``id``, ``name``, ``parent_id``, ``sort_order``,
``account_id`` under ``OrgScoped``), so the folder-tree invariants are enforced
by one implementation parameterised by the model class:

1. The parent must exist in the caller's organisation (a cross-org parent is
   rejected — FK referential actions run as the table owner and bypass RLS, so
   an unvalidated parent would open a tenant-boundary data-loss path).
2. A folder cannot be its own parent.
3. A folder cannot be moved under one of its own descendants (an ancestry
   cycle). A pre-existing cycle in the data never hangs a walk and is never
   silently extended.
4. Nesting depth cannot exceed ``MAX_FOLDER_DEPTH`` levels.

All functions assume the caller has set the RLS org context via ``set_rls_org``
before calling. The session must be within an active transaction. Validators
raise ``ValueError``; the route layer maps it to 422.
"""

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

MAX_FOLDER_DEPTH = 8
"""Maximum folder nesting depth shared by every folder model.

A single constant keeps schema and pipeline folders enforcing the same cap; the
two trees previously drifted apart (pipeline folders allowed one extra level by
comparing ``depth > MAX`` where schema folders used ``depth >= MAX``).
"""


async def compute_folder_depth(
    session: AsyncSession,
    model: type[Any],
    folder_id: uuid.UUID | None,
) -> int:
    """Return the number of levels from ``folder_id`` to the root (1 for a top-level folder).

    The walk is bounded by a visited set so a pre-existing cycle in the data can
    never hang it, and stops early once ``MAX_FOLDER_DEPTH`` is reached — callers
    only compare against the cap, so the capped value is indistinguishable from
    the true count.
    """
    depth = 0
    current_id = folder_id
    seen: set[uuid.UUID] = set()
    while current_id is not None:
        if current_id in seen:
            break
        seen.add(current_id)
        depth += 1
        if depth >= MAX_FOLDER_DEPTH:
            return depth
        result = await session.execute(select(model.parent_id).where(model.id == current_id))
        parent = result.scalar_one_or_none()
        current_id = parent
    return depth


async def folder_is_ancestor(
    session: AsyncSession,
    model: type[Any],
    folder_id: uuid.UUID,
    ancestor_id: uuid.UUID,
) -> bool:
    """Return True if ``ancestor_id`` appears in ``folder_id``'s parent chain (or equals it)."""
    current_id: uuid.UUID | None = folder_id
    seen: set[uuid.UUID] = set()
    while current_id is not None:
        if current_id == ancestor_id:
            return True
        if current_id in seen:
            return False
        seen.add(current_id)
        result = await session.execute(select(model.parent_id).where(model.id == current_id))
        parent = result.scalar_one_or_none()
        current_id = parent
    return False


async def parent_exists_in_org(session: AsyncSession, model: type[Any], parent_id: uuid.UUID) -> bool:
    """Return True if a folder with ``parent_id`` is visible in the current RLS org.

    FK checks run as the table owner and bypass RLS, so a caller who knows
    another org's folder UUID could otherwise attach a folder under a hidden
    parent. This explicit org-scoped existence check closes that hole.
    """
    result = await session.execute(select(model.id).where(model.id == parent_id))
    return result.scalar_one_or_none() is not None


async def assert_parent_exists(session: AsyncSession, model: type[Any], parent_id: uuid.UUID) -> None:
    """Reject a ``parent_id`` that does not resolve to a folder in the caller's org."""
    if not await parent_exists_in_org(session, model, parent_id):
        raise ValueError(f"Parent folder not found: {parent_id}")


async def check_parent_depth(session: AsyncSession, model: type[Any], parent_id: uuid.UUID) -> None:
    """Reject a parent whose chain would place a child beyond ``MAX_FOLDER_DEPTH`` levels."""
    if await compute_folder_depth(session, model, parent_id) >= MAX_FOLDER_DEPTH:
        raise ValueError(f"Folder nesting depth would exceed {MAX_FOLDER_DEPTH} levels")
