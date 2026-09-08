"""CRUD for SpendAnomaly records.

All functions require RLS org context to be set by the caller.
"""

import uuid
from collections.abc import Sequence
from datetime import date
from decimal import Decimal

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.models.spend_anomaly import SpendAnomaly


async def record_or_get_anomaly(
    session: AsyncSession,
    *,
    organisation_id: uuid.UUID,
    anomaly_date: date,
    amount: Decimal,
    baseline: Decimal,
    percent_above: Decimal,
    pipeline_id: uuid.UUID | None = None,
) -> SpendAnomaly:
    """Idempotently persist a freshly detected anomaly and return its row.

    The rolling-detection endpoint recomputes anomalies on every read, so a
    newly detected day must be stored before it can be acted on: a detection
    that only lives in the in-memory result has an empty ``id`` and can never
    be targeted by ``POST /anomalies/dismiss/{id}``. A repeat detection for the
    same (organisation, anomaly_date) reuses the existing row unchanged, so its
    ``dismissed`` state survives across reads. The partial unique index
    ``uq_spend_anomalies_org_date`` backs the idempotency at the storage layer.
    """
    existing = (
        await session.execute(
            select(SpendAnomaly).where(
                SpendAnomaly.organisation_id == organisation_id,
                SpendAnomaly.anomaly_date == anomaly_date,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    anomaly = SpendAnomaly(
        organisation_id=organisation_id,
        anomaly_date=anomaly_date,
        pipeline_id=pipeline_id,
        amount=amount,
        baseline=baseline,
        percent_above=percent_above,
        dismissed=False,
    )
    session.add(anomaly)
    await session.flush()
    return anomaly


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
