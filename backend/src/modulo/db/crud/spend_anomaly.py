"""CRUD for SpendAnomaly records.

All functions require RLS org context to be set by the caller.
"""

import uuid
from collections.abc import Sequence
from datetime import date
from decimal import Decimal

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
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

    The original select-then-insert path raced under concurrency: two concurrent
    first-reads of the same (organisation, anomaly_date) could both miss the row,
    both attempt the INSERT, and the loser raise ``IntegrityError`` on flush
    (uq_spend_anomalies_org_date) -> ``SQLAlchemyError`` -> a transient 503 on
    ``GET /anomalies``. We instead lead with an ``INSERT ... ON CONFLICT DO
    NOTHING`` (atomic on the storage layer, so no lost-token-style race) and then
    re-SELECT the row — whether we just inserted it or the winning concurrent
    caller did. The re-SELECT always returns the stored row, so the loser never
    observes an error.
    """
    insert_stmt = (
        pg_insert(SpendAnomaly)
        .values(
            organisation_id=organisation_id,
            anomaly_date=anomaly_date,
            pipeline_id=pipeline_id,
            amount=amount,
            baseline=baseline,
            percent_above=percent_above,
            dismissed=False,
        )
        .on_conflict_do_nothing(index_elements=["organisation_id", "anomaly_date"])
    )
    await session.execute(insert_stmt)
    anomaly = (
        await session.execute(
            select(SpendAnomaly).where(
                SpendAnomaly.organisation_id == organisation_id,
                SpendAnomaly.anomaly_date == anomaly_date,
            )
        )
    ).scalar_one()
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
