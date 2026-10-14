"""Real-engine tests for the abuse-report review lookup's org scoping (FAR-1753).

``review_abuse_report`` selected by bare ``report_id`` and leaned on RLS alone.
The fix adds an explicit ``organisation_id`` predicate (the ``crud/publisher.py``
pattern) so the lookup fails closed even on a connection that bypasses RLS.

A mocked session can only inspect the SQL text, so this module runs the lookup
against a real SQLite engine seeded with rows from TWO organisations: the
predicate is then exercised as a predicate, not as a string. SQLite is
sufficient here because the filter is dialect-independent, and the unit tier
already drives other tests from ``sqlite+aiosqlite`` engines.
"""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from modulo.db.crud.rating import review_abuse_report
from modulo.db.models.primitive_abuse_report import PrimitiveAbuseReport

_ORG_A = uuid.UUID("00000000-0000-0000-0000-00000000000a")
_ORG_B = uuid.UUID("00000000-0000-0000-0000-00000000000b")
_REVIEWER_ID = uuid.UUID("00000000-0000-0000-0000-00000000000c")


@pytest.fixture
async def engine(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'abuse_scoping.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(PrimitiveAbuseReport.__table__.create)
    yield engine
    await engine.dispose()


@pytest.fixture
async def session(engine):
    # expire_on_commit=False: an expired ORM attribute refreshes lazily, and a
    # lazy refresh outside the greenlet context raises MissingGreenlet on an
    # async session. The explicit ``_read_status`` query below still reads the
    # database, which is what each assertion needs.
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session


async def _seed_report(session: AsyncSession, *, org_id: uuid.UUID, report_id: uuid.UUID) -> None:
    session.add(
        PrimitiveAbuseReport(
            id=report_id,
            organisation_id=org_id,
            primitive_id=uuid.uuid4(),
            reason="Off-topic dig at another user",
            status="pending",
        )
    )
    await session.commit()


async def _read_status(session: AsyncSession, report_id: uuid.UUID) -> str:
    """Read ``status`` back from the database with an explicit query."""
    stmt = select(PrimitiveAbuseReport.status).where(PrimitiveAbuseReport.id == report_id)
    result = await session.execute(stmt)
    return result.scalar_one()


async def test_review_updates_a_report_of_the_callers_org(session) -> None:
    report_id = uuid.uuid4()
    await _seed_report(session, org_id=_ORG_A, report_id=report_id)

    report = await review_abuse_report(
        session,
        report_id,
        org_id=_ORG_A,
        new_status="reviewed",
        reviewer_account_id=_REVIEWER_ID,
    )
    await session.commit()

    assert report is not None
    assert report.status == "reviewed"
    assert report.reviewer_account_id == _REVIEWER_ID
    assert report.reviewed_at is not None
    assert await _read_status(session, report_id) == "reviewed"


async def test_review_ignores_a_report_belonging_to_another_org(session) -> None:
    """A report of org B is invisible to org A's reviewer — 404, no write.

    This is the fail-closed half of the fix: the lookup must not find the row
    even where RLS would already have filtered it.
    """
    report_id = uuid.uuid4()
    await _seed_report(session, org_id=_ORG_B, report_id=report_id)

    report = await review_abuse_report(
        session,
        report_id,
        org_id=_ORG_A,
        new_status="dismissed",
        reviewer_account_id=_REVIEWER_ID,
    )
    await session.commit()

    assert report is None
    assert await _read_status(session, report_id) == "pending"


async def test_review_requires_the_org_predicate(session) -> None:
    """The compiled lookup binds BOTH the report id and the caller's org.

    Pins the SQL shape itself: dropping the ``organisation_id`` predicate (the
    pre-fix behaviour) removes the bound parameter and fails here, independently
    of the row-level tests above.
    """
    report_id = uuid.uuid4()
    await _seed_report(session, org_id=_ORG_A, report_id=report_id)

    captured: list = []
    real_execute = session.execute

    async def recording_execute(*args, **kwargs):
        stmt = args[0] if args else kwargs.get("statement")
        captured.append(stmt)
        return await real_execute(*args, **kwargs)

    session.execute = recording_execute

    await review_abuse_report(session, report_id, org_id=_ORG_A, new_status="reviewed")

    compiled = captured[0].compile()
    assert "primitive_abuse_reports.organisation_id" in str(compiled)
    assert compiled.params["organisation_id_1"] == _ORG_A
    assert compiled.params["id_1"] == report_id
