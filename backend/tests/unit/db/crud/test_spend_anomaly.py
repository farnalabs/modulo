"""Unit tests for SpendAnomaly CRUD operations.

``modulo.db.crud.spend_anomaly`` was the only CRUD module with no direct test
coverage: the admin costs router (``modulo.api.routes.costs``) tests mock
``list_anomalies``/``dismiss_anomaly``, so the SQL itself — the org-scoping
WHERE clause, the ``dismissed`` filter, the date ordering, and the rowcount
semantics — was never exercised. These tests run the real statements against
an in-memory SQLite database, mirroring ``test_org_scoping.py``.
"""

import uuid
from collections.abc import AsyncGenerator
from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from modulo.db.crud.spend_anomaly import dismiss_anomaly, list_anomalies
from modulo.db.models.base import Base
from modulo.db.models.spend_anomaly import SpendAnomaly

_ORG_A = uuid.UUID("00000000-0000-0000-0000-00000000000a")
_ORG_B = uuid.UUID("00000000-0000-0000-0000-00000000000b")


@pytest.fixture
async def engine() -> AsyncGenerator[AsyncEngine, None]:
    eng = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with eng.begin() as conn:
        await conn.exec_driver_sql("PRAGMA foreign_keys = OFF")
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=[SpendAnomaly.__table__]))
    yield eng
    await eng.dispose()


@pytest.fixture
async def session(engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s


def _make_anomaly(
    *,
    organisation_id: uuid.UUID,
    anomaly_date: date,
    amount: str = "500.00",
    baseline: str = "200.00",
    percent_above: str = "150.00",
    dismissed: bool = False,
) -> SpendAnomaly:
    return SpendAnomaly(
        organisation_id=organisation_id,
        anomaly_date=anomaly_date,
        amount=Decimal(amount),
        baseline=Decimal(baseline),
        percent_above=Decimal(percent_above),
        dismissed=dismissed,
    )


async def _seed(
    session: AsyncSession,
    *anomalies: SpendAnomaly,
) -> list[SpendAnomaly]:
    session.add_all(anomalies)
    await session.flush()
    return list(anomalies)


class TestListAnomalies:
    async def test_returns_only_anomalies_for_org(self, session: AsyncSession) -> None:
        org_a = await _seed(
            session,
            _make_anomaly(organisation_id=_ORG_A, anomaly_date=date(2025, 6, 1)),
            _make_anomaly(organisation_id=_ORG_A, anomaly_date=date(2025, 6, 2)),
        )
        await _seed(session, _make_anomaly(organisation_id=_ORG_B, anomaly_date=date(2025, 6, 3)))

        result = await list_anomalies(session, organisation_id=_ORG_A)
        assert len(result) == 2
        assert {a.id for a in result} == {a.id for a in org_a}

    async def test_orders_by_anomaly_date_descending(self, session: AsyncSession) -> None:
        await _seed(
            session,
            _make_anomaly(organisation_id=_ORG_A, anomaly_date=date(2025, 6, 1)),
            _make_anomaly(organisation_id=_ORG_A, anomaly_date=date(2025, 6, 3)),
            _make_anomaly(organisation_id=_ORG_A, anomaly_date=date(2025, 6, 2)),
        )

        result = await list_anomalies(session, organisation_id=_ORG_A)
        dates = [a.anomaly_date for a in result]
        assert dates == [date(2025, 6, 3), date(2025, 6, 2), date(2025, 6, 1)]

    async def test_dismissed_none_returns_all(self, session: AsyncSession) -> None:
        await _seed(
            session,
            _make_anomaly(organisation_id=_ORG_A, anomaly_date=date(2025, 6, 1), dismissed=False),
            _make_anomaly(organisation_id=_ORG_A, anomaly_date=date(2025, 6, 2), dismissed=True),
        )

        result = await list_anomalies(session, organisation_id=_ORG_A, dismissed=None)
        assert len(result) == 2

    async def test_dismissed_false_excludes_dismissed(self, session: AsyncSession) -> None:
        await _seed(
            session,
            _make_anomaly(organisation_id=_ORG_A, anomaly_date=date(2025, 6, 1), dismissed=False),
            _make_anomaly(organisation_id=_ORG_A, anomaly_date=date(2025, 6, 2), dismissed=True),
        )

        result = await list_anomalies(session, organisation_id=_ORG_A, dismissed=False)
        assert len(result) == 1
        assert result[0].dismissed is False

    async def test_dismissed_true_returns_only_dismissed(self, session: AsyncSession) -> None:
        await _seed(
            session,
            _make_anomaly(organisation_id=_ORG_A, anomaly_date=date(2025, 6, 1), dismissed=False),
            _make_anomaly(organisation_id=_ORG_A, anomaly_date=date(2025, 6, 2), dismissed=True),
        )

        result = await list_anomalies(session, organisation_id=_ORG_A, dismissed=True)
        assert len(result) == 1
        assert result[0].dismissed is True

    async def test_returns_empty_when_no_anomalies(self, session: AsyncSession) -> None:
        result = await list_anomalies(session, organisation_id=_ORG_A)
        assert result == []


class TestDismissAnomaly:
    async def test_dismisses_own_org_anomaly(self, session: AsyncSession) -> None:
        [anomaly] = await _seed(session, _make_anomaly(organisation_id=_ORG_A, anomaly_date=date(2025, 6, 1)))

        dismissed = await dismiss_anomaly(session, anomaly_id=anomaly.id, organisation_id=_ORG_A)
        assert dismissed is True

        await session.refresh(anomaly)
        assert anomaly.dismissed is True

    async def test_returns_false_for_other_org_anomaly(self, session: AsyncSession) -> None:
        [anomaly] = await _seed(session, _make_anomaly(organisation_id=_ORG_A, anomaly_date=date(2025, 6, 1)))

        dismissed = await dismiss_anomaly(session, anomaly_id=anomaly.id, organisation_id=_ORG_B)
        assert dismissed is False

        await session.refresh(anomaly)
        assert anomaly.dismissed is False

    async def test_returns_false_when_not_found(self, session: AsyncSession) -> None:
        dismissed = await dismiss_anomaly(
            session,
            anomaly_id=uuid.uuid4(),
            organisation_id=_ORG_A,
        )
        assert dismissed is False

    async def test_flush_errors_propagate(self, session: AsyncSession) -> None:
        [anomaly] = await _seed(session, _make_anomaly(organisation_id=_ORG_A, anomaly_date=date(2025, 6, 1)))

        class _FlushError(Exception):
            pass

        session.flush = AsyncMock(side_effect=_FlushError("flush failed"))  # type: ignore[method-assign]
        with pytest.raises(_FlushError):
            await dismiss_anomaly(session, anomaly_id=anomaly.id, organisation_id=_ORG_A)

    async def test_rowcountless_result_falls_back_to_true(self) -> None:
        result = MagicMock()
        del result.rowcount
        session = AsyncMock()
        session.execute.return_value = result

        from modulo.db.crud import spend_anomaly as crud

        dismissed = await crud.dismiss_anomaly(
            session,
            anomaly_id=uuid.uuid4(),
            organisation_id=_ORG_A,
        )
        assert dismissed is True
        session.flush.assert_awaited_once()
