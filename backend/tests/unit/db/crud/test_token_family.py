"""Unit tests for token family CRUD (JWT refresh rotation + family invalidation).

Covers the user-offboarding product-map "Token Blacklisting Details" behaviours:
``list_families_for_account``, ``blacklist_family`` (incl. the no-op path for a
missing family), and ``advance_sequence`` theft detection for a blacklisted or
out-of-order family. Uses an in-memory SQLite engine (no Docker, no Postgres) —
the same pattern as ``test_org_scoping.py``.
"""

import uuid
from collections.abc import AsyncGenerator

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from modulo.db.crud.token_family import (
    advance_sequence,
    blacklist_family,
    list_families_for_account,
)
from modulo.db.models.base import Base
from modulo.db.models.token_family import TokenFamily

_ORG_A = uuid.UUID("00000000-0000-0000-0000-00000000000a")
_ACCOUNT_A = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
_ACCOUNT_B = uuid.UUID("00000000-0000-0000-0000-0000000000b1")


@pytest.fixture
async def engine() -> AsyncGenerator[AsyncEngine, None]:
    eng = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=[TokenFamily.__table__]))
        await conn.exec_driver_sql("PRAGMA foreign_keys = OFF")
    yield eng
    await eng.dispose()


@pytest.fixture
async def session(engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s


async def _seed_family(session: AsyncSession, *, account_id: uuid.UUID, org_id: uuid.UUID) -> TokenFamily:
    family = TokenFamily(
        family_id=uuid.uuid4(),
        account_id=account_id,
        organisation_id=org_id,
        max_sequence=0,
    )
    session.add(family)
    await session.flush()
    return family


class TestListFamiliesForAccount:
    async def test_returns_all_families_for_account(self, session: AsyncSession) -> None:
        first = await _seed_family(session, account_id=_ACCOUNT_A, org_id=_ORG_A)
        second = await _seed_family(session, account_id=_ACCOUNT_A, org_id=_ORG_A)

        families = await list_families_for_account(session, _ACCOUNT_A)

        assert {f.family_id for f in families} == {first.family_id, second.family_id}

    async def test_does_not_return_other_accounts_families(self, session: AsyncSession) -> None:
        await _seed_family(session, account_id=_ACCOUNT_B, org_id=_ORG_A)

        families = await list_families_for_account(session, _ACCOUNT_A)

        assert families == []


class TestBlacklistFamily:
    async def test_sets_blacklisted_and_blacklisted_at(self, session: AsyncSession) -> None:
        family = await _seed_family(session, account_id=_ACCOUNT_A, org_id=_ORG_A)
        assert family.is_blacklisted is False
        assert family.blacklisted_at is None

        ok = await blacklist_family(session, family.family_id, _ACCOUNT_A)

        assert ok is True
        assert family.is_blacklisted is True
        assert family.blacklisted_at is not None

    async def test_missing_family_returns_false_noop(self, session: AsyncSession) -> None:
        ok = await blacklist_family(session, uuid.uuid4(), _ACCOUNT_A)

        assert ok is False

    async def test_already_blacklisted_returns_true(self, session: AsyncSession) -> None:
        family = await _seed_family(session, account_id=_ACCOUNT_A, org_id=_ORG_A)
        await blacklist_family(session, family.family_id, _ACCOUNT_A)

        ok = await blacklist_family(session, family.family_id, _ACCOUNT_A)

        assert ok is True
        assert family.is_blacklisted is True


class TestAdvanceSequenceTheftDetection:
    async def test_blacklisted_family_returns_theft_detected(self, session: AsyncSession) -> None:
        family = await _seed_family(session, account_id=_ACCOUNT_A, org_id=_ORG_A)
        await blacklist_family(session, family.family_id, _ACCOUNT_A)

        new_sequence, theft_detected = await advance_sequence(
            session, family.family_id, family.max_sequence, _ACCOUNT_A
        )

        assert theft_detected is True
        assert new_sequence == 0

    async def test_sequence_mismatch_blacklists_and_returns_theft_detected(self, session: AsyncSession) -> None:
        family = await _seed_family(session, account_id=_ACCOUNT_A, org_id=_ORG_A)

        _, theft_detected = await advance_sequence(session, family.family_id, 99, _ACCOUNT_A)

        assert theft_detected is True
        assert family.is_blacklisted is True
        assert family.blacklisted_at is not None

    async def test_expected_sequence_advances_cleanly(self, session: AsyncSession) -> None:
        family = await _seed_family(session, account_id=_ACCOUNT_A, org_id=_ORG_A)
        previous_sequence = family.max_sequence

        new_sequence, theft_detected = await advance_sequence(session, family.family_id, previous_sequence, _ACCOUNT_A)

        assert theft_detected is False
        assert new_sequence == previous_sequence + 1

    async def test_missing_family_returns_no_theft(self, session: AsyncSession) -> None:
        new_sequence, theft_detected = await advance_sequence(session, uuid.uuid4(), 0, _ACCOUNT_A)

        assert (new_sequence, theft_detected) == (0, False)
