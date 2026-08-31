"""CRUD for NodeObservation records.

All functions require RLS org context to be set by the caller.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.models.node_observation import NodeObservation
from modulo.utils.uuid import coerce_uuid


async def observe_node(
    session: AsyncSession,
    *,
    organisation_id: uuid.UUID,
    run_id: uuid.UUID,
    node_id: str,
    observed_by: uuid.UUID | None = None,
) -> NodeObservation:
    """Record that a human has observed a node's output.

    If an observation already exists for this (run_id, node_id) pair,
    it is returned unchanged (idempotent).
    """
    result = await session.execute(
        select(NodeObservation)
        .where(
            NodeObservation.run_id == run_id,
            NodeObservation.node_id == node_id,
            NodeObservation.organisation_id == organisation_id,
        )
        .with_for_update()
    )
    existing = result.scalar_one_or_none()
    if existing is not None:
        return existing

    obs = NodeObservation(
        id=uuid.uuid4(),
        organisation_id=organisation_id,
        run_id=run_id,
        node_id=coerce_uuid(node_id),
        human_observed_by=observed_by,
        human_observed_at=datetime.now(UTC),
    )
    session.add(obs)
    await session.flush()
    return obs
