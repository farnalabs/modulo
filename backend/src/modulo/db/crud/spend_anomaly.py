"""CRUD for SpendAnomaly records.

All functions require RLS org context to be set by the caller.
"""

import uuid
from collections.abc import Sequence

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.models.spend_anomaly import SpendAnomaly


async def list_anomalies(
    session: AsyncSession,
    *,
    organisation_id: uuid.UUID,
    dismissed: bool | None = None,
) -> Sequence[SpendAnomaly]:
    q = select(SpendAnomaly).where(
        SpendAnomaly.organisation_id == organisation_id,
    )
    if dismissed is not None:
        q = q.where(SpendAnomaly.dismissed == dismissed)
    q = q.order_by(SpendAnomaly.anomaly_date.desc())
    result = await session.execute(q)
    return list(result.scalars().all())


async def dismiss_anomaly(
    session: AsyncSession,
    *,
    anomaly_id: uuid.UUID,
    organisation_id: uuid.UUID,
) -> bool:
    q = (
        update(SpendAnomaly)
        .where(
            SpendAnomaly.id == anomaly_id,
            SpendAnomaly.organisation_id == organisation_id,
        )
        .values(dismissed=True)
    )
    result = await session.execute(q)
    await session.flush()
    return bool(result.rowcount > 0) if hasattr(result, "rowcount") else True
