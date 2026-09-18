"""Postgres-backed integration tests for SpendAnomaly CRUD.

The partial unique index ``uq_spend_anomalies_org_date`` is declared with
``WHERE pipeline_id IS NULL``. SQLite (and MariaDB) ignore the predicate, so the
unit suite can never surface the Postgres-specific ``42P10`` raised when
``ON CONFLICT DO NOTHING`` omits ``index_where`` and Postgres cannot infer the
partial index as the conflict arbiter. These tests run the real statements
against a migrated Postgres so the inference is exercised end to end.
"""

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.spend_anomaly import record_or_get_anomaly

pytestmark = pytest.mark.integration

_DATE = date(2025, 6, 1)


async def test_first_insert_succeeds_against_partial_index(
    rls_session: AsyncSession,
    test_org: uuid.UUID,
) -> None:
    anomaly = await record_or_get_anomaly(
        rls_session,
        organisation_id=test_org,
        anomaly_date=_DATE,
        amount=Decimal("500.00"),
        baseline=Decimal("200.00"),
        percent_above=Decimal("150.00"),
    )
    assert anomaly.id is not None
    assert anomaly.dismissed is False


async def test_repeat_detection_reuses_existing_row(
    rls_session: AsyncSession,
    test_org: uuid.UUID,
) -> None:
    first = await record_or_get_anomaly(
        rls_session,
        organisation_id=test_org,
        anomaly_date=_DATE,
        amount=Decimal("500.00"),
        baseline=Decimal("200.00"),
        percent_above=Decimal("150.00"),
    )
    second = await record_or_get_anomaly(
        rls_session,
        organisation_id=test_org,
        anomaly_date=_DATE,
        amount=Decimal("900.00"),
        baseline=Decimal("100.00"),
        percent_above=Decimal("800.00"),
    )
    # ON CONFLICT DO NOTHING must reuse the first row unchanged (dismissed
    # state preserved), not raise 42P10 on the partial-index inference.
    assert second.id == first.id
    assert second.dismissed is False
