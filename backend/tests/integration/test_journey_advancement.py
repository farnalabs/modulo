"""Integration: journey advancement executes against REAL Postgres (FAR-665).

The unit suite runs on SQLite, which silently accepted the ISO *string* the
advancer used to bind for ``:evidence_ts`` — Postgres infers that parameter
as ``timestamptz`` (from the ``:evidence_ts > journeys.updated_at``
compare-and-set) and asyncpg refuses to encode a Python ``str`` into it
(``DataError: invalid input for query argument``), so EVERY
``advance_journeys`` call failed on production and journeys never advanced
(FAR-665; every caller is fail-open, so nothing surfaced beyond logs). Only a
real Postgres engine can catch this class of bug: these tests drive the real
upsert through asyncpg and assert the row actually advanced, then pin the
compare-and-set evidence semantics post-fix (newer evidence overwrites,
older and equal evidence ignored) and the RLS org scoping the docstring's
call contract promises.

Sessions run as the production ``modulo_app`` role (NOBYPASSRLS — the same
``SET ROLE`` posture the REST routes use), with RLS org context set to
``test_org``: the ``rls_org_isolation`` policy on ``journeys`` is genuinely
enforced here, not bypassed by the testcontainers superuser.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from modulo.core.lifecycle_map.advancement import advance_journeys
from modulo.db.lifecycle_refs import canonical_work_item_id
from modulo.db.models.journey import Journey
from modulo.db.rls import set_rls_org

pytestmark = pytest.mark.integration

# Fixed evidence timestamps (UTC) — no dependence on the wall clock.
_T0 = datetime(2026, 1, 1, tzinfo=UTC)
_T1 = datetime(2026, 1, 2, tzinfo=UTC)
_T2 = datetime(2026, 1, 3, tzinfo=UTC)
_T3 = datetime(2026, 1, 4, tzinfo=UTC)

_REFS = [{"kind": "pr", "ref": "#123", "source": "derived"}]


@pytest_asyncio.fixture
async def app_role_rls_session(
    modulo_app_engine: AsyncEngine,
    test_org: uuid.UUID,
) -> AsyncGenerator[AsyncSession, None]:
    """AsyncSession as the production ``modulo_app`` role (RLS enforced).

    ``SET ROLE modulo_app`` makes ``current_user`` a NOBYPASSRLS non-owner, so
    the ``rls_org_isolation`` policy on ``journeys`` actually filters — unlike
    the testcontainers superuser, which bypasses RLS even under FORCE. Org
    context is set transaction-locally (``set_rls_org``), mirroring
    ``advance_journeys``'s documented RLS/transaction contract; all writes
    roll back at teardown.
    """
    factory = async_sessionmaker(modulo_app_engine, expire_on_commit=False)
    async with factory() as session:
        await session.execute(text("SELECT 1"))
        await set_rls_org(session, test_org)
        yield session
        await session.rollback()


async def _seed_journey(
    session: AsyncSession,
    organisation_id: uuid.UUID,
    *,
    latest_terminal_run_id: uuid.UUID,
    latest_status: str | None = "complete",
    updated_at: datetime,
) -> Journey:
    journey = Journey(
        organisation_id=organisation_id,
        kind="pr",
        ref="123",
        canonical_work_item_id=canonical_work_item_id(organisation_id, "pr", "123"),
        run_count=1,
        latest_terminal_run_id=latest_terminal_run_id,
        latest_status=latest_status,
        latest_provenance="derived",
        created_at=_T0,
        updated_at=updated_at,
    )
    session.add(journey)
    await session.flush()
    return journey


async def _read_journey(session: AsyncSession, organisation_id: uuid.UUID) -> Journey | None:
    # The raw-SQL advance writes through the same session's connection; expire
    # the identity map so the reload reflects the advanced values, not the
    # seeded object.
    session.expire_all()
    return (
        await session.execute(
            select(Journey).where(
                Journey.organisation_id == organisation_id,
                Journey.kind == "pr",
                Journey.ref == "123",
            )
        )
    ).scalar_one_or_none()


async def _advance(
    session: AsyncSession,
    organisation_id: uuid.UUID,
    *,
    run_id: uuid.UUID,
    status: str,
    completed_at: datetime,
) -> int:
    return await advance_journeys(
        session,
        organisation_id,
        run_id=run_id,
        pipeline_id=None,
        refs=_REFS,
        status=status,
        completed_at=completed_at,
        run_created_at=_T1,
    )


async def test_first_advance_inserts_journey_with_evidence(
    app_role_rls_session: AsyncSession, test_org: uuid.UUID
) -> None:
    """FAR-665 regression: the upsert must EXECUTE on Postgres (the str bind
    raised ``asyncpg.DataError`` here — silently swallowed upstream) and the
    INSERT arm must land evidence + run_count."""
    run_id = uuid.uuid4()
    advanced = await _advance(app_role_rls_session, test_org, run_id=run_id, status="complete", completed_at=_T2)
    assert advanced == 1

    journey = await _read_journey(app_role_rls_session, test_org)
    assert journey is not None
    assert journey.run_count == 1
    assert journey.latest_terminal_run_id == run_id
    assert journey.latest_status == "complete"
    assert journey.latest_provenance == "derived"
    assert journey.updated_at == _T2


async def test_older_evidence_is_ignored_by_compare_and_set(
    app_role_rls_session: AsyncSession, test_org: uuid.UUID
) -> None:
    first_run = uuid.uuid4()
    await _seed_journey(app_role_rls_session, test_org, latest_terminal_run_id=first_run, updated_at=_T2)

    older_run = uuid.uuid4()
    advanced = await _advance(app_role_rls_session, test_org, run_id=older_run, status="complete", completed_at=_T1)
    assert advanced == 1

    journey = await _read_journey(app_role_rls_session, test_org)
    assert journey is not None
    assert journey.run_count == 2
    assert journey.latest_terminal_run_id == first_run
    assert journey.latest_status == "complete"
    assert journey.updated_at == _T2


async def test_equal_evidence_keeps_existing_evidence(app_role_rls_session: AsyncSession, test_org: uuid.UUID) -> None:
    first_run = uuid.uuid4()
    await _seed_journey(app_role_rls_session, test_org, latest_terminal_run_id=first_run, updated_at=_T2)

    equal_run = uuid.uuid4()
    advanced = await _advance(app_role_rls_session, test_org, run_id=equal_run, status="complete", completed_at=_T2)
    assert advanced == 1

    journey = await _read_journey(app_role_rls_session, test_org)
    assert journey is not None
    assert journey.run_count == 2
    assert journey.latest_terminal_run_id == first_run
    assert journey.updated_at == _T2


async def test_newer_evidence_overwrites(app_role_rls_session: AsyncSession, test_org: uuid.UUID) -> None:
    first_run = uuid.uuid4()
    await _seed_journey(app_role_rls_session, test_org, latest_terminal_run_id=first_run, updated_at=_T1)

    newer_run = uuid.uuid4()
    advanced = await _advance(app_role_rls_session, test_org, run_id=newer_run, status="failed", completed_at=_T3)
    assert advanced == 1

    journey = await _read_journey(app_role_rls_session, test_org)
    assert journey is not None
    assert journey.run_count == 2
    assert journey.latest_terminal_run_id == newer_run
    assert journey.latest_status == "failed"
    assert journey.updated_at == _T3


async def test_journey_row_is_rls_scoped_to_the_calling_org(
    app_role_rls_session: AsyncSession, test_org: uuid.UUID
) -> None:
    """The advance is visible only under the advancing org's RLS context.

    Both probes run the SAME unfiltered query (no ``organisation_id``
    predicate) as the NOBYPASSRLS ``modulo_app`` role — only the
    ``app.organisation_id`` GUC changes between them, so the differing
    results are decided by the ``rls_org_isolation`` policy, not the query.
    """
    run_id = uuid.uuid4()
    advanced = await _advance(app_role_rls_session, test_org, run_id=run_id, status="complete", completed_at=_T2)
    assert advanced == 1

    async def _unfiltered_journey() -> Journey | None:
        app_role_rls_session.expire_all()
        return (
            await app_role_rls_session.execute(select(Journey).where(Journey.kind == "pr", Journey.ref == "123"))
        ).scalar_one_or_none()

    visible = await _unfiltered_journey()
    assert visible is not None
    assert visible.run_count == 1

    other_org = uuid.uuid4()
    await set_rls_org(app_role_rls_session, other_org)
    invisible = await _unfiltered_journey()
    assert invisible is None
