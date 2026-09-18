"""Org-scoped CRUD for ConnectorInstance.

All functions require RLS org context to be set by the caller.
"""

import uuid
from collections.abc import Collection
from typing import Any

from sqlalchemy import case, func, select, update
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.base import PageResult, apply_updates
from modulo.db.crud.pagination import CursorPaginator
from modulo.db.models.connector_instance import ConnectorInstance

# Matches the hub's _SKIP_SUMMARY_LIMIT / _record_skip sanitization (FAR-495):
# Postgres rejects NUL bytes in SQL text (a NUL in any batched summary fails the
# WHOLE UPDATE so no instance gets marked) and the column is String(2000).
_SKIP_SUMMARY_LIMIT: int = 2000


def _sanitize_skip_summary(summary: str) -> str:
    """NUL-strip + truncate a skip summary so the batched UPDATE cannot fail (FAR-498).

    Defense-in-depth twin of ``ConnectorHub._record_skip``: the hub sanitizes
    the summaries it records, but this writer must not trust every future
    caller to pass DB-safe values. Kept consistent with the hub's logic
    (NUL-strip, then truncate to 2000 — the ``last_skip_error`` String(2000)
    column limit). Module-level here because db must not import core.
    """
    return summary.replace("\x00", "")[:_SKIP_SUMMARY_LIMIT]


async def create_connector_instance(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    name: str,
    connector_type_id: str,
    account_id: uuid.UUID,
    credentials_ciphertext: bytes,
    config_json: dict[str, Any] | None = None,
    allowed_operations: list[str] | None = None,
    visibility: str = "org",
    owner_team_id: uuid.UUID | None = None,
    tier: str = "native",
) -> ConnectorInstance:
    ci = ConnectorInstance(
        organisation_id=org_id,
        name=name,
        connector_type_id=connector_type_id,
        account_id=account_id,
        credentials_ciphertext=credentials_ciphertext,
        config_json=config_json or {},
        allowed_operations=allowed_operations or [],
        visibility=visibility,
        owner_team_id=owner_team_id,
        tier=tier,
    )
    session.add(ci)
    await session.flush()
    return ci


async def get_connector_instance(session: AsyncSession, connector_id: uuid.UUID) -> ConnectorInstance | None:
    result = await session.execute(select(ConnectorInstance).where(ConnectorInstance.id == connector_id))
    return result.scalar_one_or_none()


async def list_connector_instances(
    session: AsyncSession,
    *,
    organisation_id: uuid.UUID | None = None,
    page: int = 1,
    page_size: int = 20,
    cursor: str | None = None,
    excluded_tiers: list[str] | None = None,
) -> PageResult[ConnectorInstance]:
    if excluded_tiers is None:
        excluded_tiers = ["in_dev"]

    org_filter = ConnectorInstance.organisation_id == organisation_id if organisation_id else None

    if cursor is not None:
        stmt = select(ConnectorInstance)
        if org_filter is not None:
            stmt = stmt.where(org_filter)
        if excluded_tiers:
            stmt = stmt.where(~ConnectorInstance.tier.in_(excluded_tiers))
        paginator = CursorPaginator()
        cp = await paginator.paginate(
            session,
            stmt,
            cursor=cursor,
            limit=page_size,
            model=ConnectorInstance,
            compute_total=True,
        )
        return PageResult(
            items=cp.items,
            total=cp.total or 0,
            page=page,
            page_size=page_size,
            next_cursor=cp.next_cursor,
            has_more=cp.has_more,
        )

    offset = (page - 1) * page_size
    total_query = select(func.count()).select_from(ConnectorInstance)
    if org_filter is not None:
        total_query = total_query.where(org_filter)
    if excluded_tiers:
        total_query = total_query.where(~ConnectorInstance.tier.in_(excluded_tiers))
    try:
        total = (await session.execute(total_query)).scalar_one()
    except ProgrammingError:
        return PageResult(items=[], total=0, page=page, page_size=page_size)
    try:
        items_stmt = (
            select(ConnectorInstance).order_by(ConnectorInstance.created_at.desc()).offset(offset).limit(page_size)
        )
        if org_filter is not None:
            items_stmt = items_stmt.where(org_filter)
        if excluded_tiers:
            items_stmt = items_stmt.where(~ConnectorInstance.tier.in_(excluded_tiers))
        items = list((await session.execute(items_stmt)).scalars())
    except ProgrammingError:
        return PageResult(items=[], total=0, page=page, page_size=page_size)
    return PageResult(items=items, total=total, page=page, page_size=page_size)


async def update_connector_instance(
    session: AsyncSession,
    connector_id: uuid.UUID,
    updates: dict[str, Any],
) -> ConnectorInstance | None:
    """Apply a partial update to a connector instance.

    ``updates`` may carry ``credentials_ciphertext`` as a PARTIAL credential
    update — but only for the REST connector, where the route overlays the
    supplied credential identity (non-secret) fields onto the decrypted stored
    credential and re-encrypts, so an identity-only edit applies while any
    absent/empty secret field is left intact (FAR-466). For every other
    connector the route writes a FULL-REPLACE ciphertext (no overlay). This CRUD
    just persists whatever ciphertext it is given.
    """
    ci = await get_connector_instance(session, connector_id)
    if ci is None:
        return None
    apply_updates(ci, updates)
    await session.flush()
    return ci


async def mark_instances_degraded(session: AsyncSession, skipped: dict[uuid.UUID, str]) -> None:
    """Persist degraded_at/last_skip_error for instances that failed hub initialisation (FAR-495).

    Issues a single ``UPDATE connector_instances SET degraded_at = now(),
    last_skip_error = <summary> WHERE id = ANY(<ids>)`` statement. An empty
    *skipped* dict is a no-op. Each summary is sanitized writer-side
    (NUL-strip + truncate to 2000, FAR-498) so a caller bypassing the hub's
    own sanitization cannot overflow the String(2000) column or fail the whole
    batched UPDATE with a NUL byte. Requires RLS org context to be set by the
    caller.
    """
    if not skipped:
        return
    summary_by_id = case(
        *[
            (ConnectorInstance.id == instance_id, _sanitize_skip_summary(summary))
            for instance_id, summary in skipped.items()
        ],
    )
    stmt = (
        update(ConnectorInstance)
        .where(ConnectorInstance.id.in_(skipped))
        .values(degraded_at=func.now(), last_skip_error=summary_by_id)
    )
    await session.execute(stmt)
    await session.flush()


async def clear_degraded_markers(session: AsyncSession, instance_ids: Collection[uuid.UUID]) -> None:
    """Clear degraded_at/last_skip_error for instances that initialised successfully (FAR-495).

    Compensates :func:`mark_instances_degraded`: a connector fixed via a config
    or plugin change (not a credential update) stops being flagged degraded
    once the hub successfully initialises it. Issues a single ``UPDATE
    connector_instances SET degraded_at = NULL, last_skip_error = NULL WHERE id
    = ANY(<ids>)`` statement. An empty *instance_ids* collection is a no-op.
    Only instances actually attempted and initialised by the hub are passed in,
    so out-of-scope instances are never touched. Requires RLS org context to be
    set by the caller.
    """
    if not instance_ids:
        return
    stmt = (
        update(ConnectorInstance)
        .where(ConnectorInstance.id.in_(instance_ids))
        .values(degraded_at=None, last_skip_error=None)
    )
    await session.execute(stmt)
    await session.flush()


async def delete_connector_instance(session: AsyncSession, connector_id: uuid.UUID) -> bool:
    ci = await get_connector_instance(session, connector_id)
    if ci is None:
        return False
    await session.delete(ci)
    await session.flush()
    return True
