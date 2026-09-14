"""Unit tests for token family CRUD (JWT refresh rotation + family invalidation).

Covers the user-offboarding product-map "Token Blacklisting Details" behaviours:
``list_families_for_account``, ``blacklist_family`` (incl. the no-op path for a
missing family), and ``advance_sequence`` theft detection for a blacklisted or
out-of-order family. Uses an in-memory SQLite engine (no Docker, no Postgres) —
the same pattern as ``test_org_scoping.py``.
"""

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

import modulo.db.crud.token_family as token_family_module
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

# FAR-819 default reuse-grace config for tests that drive explicit maturities.
_GRACE_SECONDS = 30
_GRACE_MAX_STEPS = 3
_GRACE_MAX_PER_WINDOW = 8


def _freeze_now(monkeypatch: pytest.MonkeyPatch, now: datetime) -> None:
    """Pin ``token_family_module.datetime.now`` so the reuse-window math is deterministic."""

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return now.astimezone(tz) if tz is not None else now

    monkeypatch.setattr(token_family_module, "datetime", _FrozenDatetime)


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

        new_sequence, theft_detected, grace_replay = await advance_sequence(
            session,
            family.family_id,
            family.max_sequence,
            _ACCOUNT_A,
            reuse_grace_seconds=_GRACE_SECONDS,
            reuse_grace_max_steps=_GRACE_MAX_STEPS,
            reuse_grace_max_per_window=_GRACE_MAX_PER_WINDOW,
        )

        assert theft_detected is True
        assert grace_replay is False
        assert new_sequence == 0

    async def test_sequence_mismatch_ahead_of_max_blacklists(self, session: AsyncSession) -> None:
        family = await _seed_family(session, account_id=_ACCOUNT_A, org_id=_ORG_A)

        _, theft_detected, grace_replay = await advance_sequence(
            session,
            family.family_id,
            99,
            _ACCOUNT_A,
            reuse_grace_seconds=_GRACE_SECONDS,
            reuse_grace_max_steps=_GRACE_MAX_STEPS,
            reuse_grace_max_per_window=_GRACE_MAX_PER_WINDOW,
        )

        assert theft_detected is True
        assert grace_replay is False
        assert family.is_blacklisted is True
        assert family.blacklisted_at is not None

    async def test_expected_sequence_advances_cleanly(self, session: AsyncSession) -> None:
        family = await _seed_family(session, account_id=_ACCOUNT_A, org_id=_ORG_A)
        previous_sequence = family.max_sequence

        new_sequence, theft_detected, grace_replay = await advance_sequence(
            session,
            family.family_id,
            previous_sequence,
            _ACCOUNT_A,
            reuse_grace_seconds=_GRACE_SECONDS,
            reuse_grace_max_steps=_GRACE_MAX_STEPS,
            reuse_grace_max_per_window=_GRACE_MAX_PER_WINDOW,
        )

        assert theft_detected is False
        assert grace_replay is False
        assert new_sequence == previous_sequence + 1

    async def test_missing_family_returns_no_theft(self, session: AsyncSession) -> None:
        new_sequence, theft_detected, grace_replay = await advance_sequence(
            session,
            uuid.uuid4(),
            0,
            _ACCOUNT_A,
            reuse_grace_seconds=_GRACE_SECONDS,
            reuse_grace_max_steps=_GRACE_MAX_STEPS,
            reuse_grace_max_per_window=_GRACE_MAX_PER_WINDOW,
        )

        assert (new_sequence, theft_detected, grace_replay) == (0, False, False)


class TestAdvanceSequenceReuseGraceWindow:
    async def _seed_stale_family(
        self,
        session: AsyncSession,
        *,
        max_sequence: int,
        rotated_at: datetime,
        reuse_window_started_at: datetime | None = None,
        reuse_replay_count: int = 0,
    ) -> TokenFamily:
        family = await _seed_family(session, account_id=_ACCOUNT_A, org_id=_ORG_A)
        family.max_sequence = max_sequence
        family.rotated_at = rotated_at
        family.reuse_window_started_at = reuse_window_started_at
        family.reuse_replay_count = reuse_replay_count
        await session.flush()
        return family

    async def test_reuse_within_window_one_step_behind_is_benign(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        now = datetime.now(UTC)
        _freeze_now(monkeypatch, now)
        family = await self._seed_stale_family(
            session,
            max_sequence=5,
            rotated_at=now - timedelta(seconds=5),
        )

        new_sequence, theft_detected, grace_replay = await advance_sequence(
            session,
            family.family_id,
            4,
            _ACCOUNT_A,
            reuse_grace_seconds=_GRACE_SECONDS,
            reuse_grace_max_steps=_GRACE_MAX_STEPS,
            reuse_grace_max_per_window=_GRACE_MAX_PER_WINDOW,
        )

        assert theft_detected is False
        assert grace_replay is True
        assert new_sequence == 6
        assert family.is_blacklisted is False
        assert family.reuse_replay_count == 1

    async def test_reuse_two_steps_behind_within_tolerance_is_benign(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        now = datetime.now(UTC)
        _freeze_now(monkeypatch, now)
        family = await self._seed_stale_family(
            session,
            max_sequence=5,
            rotated_at=now - timedelta(seconds=5),
        )

        new_sequence, theft_detected, grace_replay = await advance_sequence(
            session,
            family.family_id,
            3,
            _ACCOUNT_A,
            reuse_grace_seconds=_GRACE_SECONDS,
            reuse_grace_max_steps=_GRACE_MAX_STEPS,
            reuse_grace_max_per_window=_GRACE_MAX_PER_WINDOW,
        )

        assert theft_detected is False
        assert grace_replay is True
        assert new_sequence == 6

    async def test_reuse_beyond_steps_tolerance_blacklists(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        now = datetime.now(UTC)
        _freeze_now(monkeypatch, now)
        family = await self._seed_stale_family(
            session,
            max_sequence=5,
            rotated_at=now - timedelta(seconds=5),
        )

        _, theft_detected, grace_replay = await advance_sequence(
            session,
            family.family_id,
            1,
            _ACCOUNT_A,
            reuse_grace_seconds=_GRACE_SECONDS,
            reuse_grace_max_steps=_GRACE_MAX_STEPS,
            reuse_grace_max_per_window=_GRACE_MAX_PER_WINDOW,
        )

        assert theft_detected is True
        assert grace_replay is False
        assert family.is_blacklisted is True

    async def test_reuse_with_grace_disabled_blacklists(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        now = datetime.now(UTC)
        _freeze_now(monkeypatch, now)
        family = await self._seed_stale_family(
            session,
            max_sequence=5,
            rotated_at=now - timedelta(seconds=5),
        )

        _, theft_detected, grace_replay = await advance_sequence(
            session,
            family.family_id,
            4,
            _ACCOUNT_A,
            reuse_grace_seconds=0,
            reuse_grace_max_steps=_GRACE_MAX_STEPS,
            reuse_grace_max_per_window=_GRACE_MAX_PER_WINDOW,
        )

        assert theft_detected is True
        assert grace_replay is False
        assert family.is_blacklisted is True

    async def test_reuse_with_expired_window_blacklists(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        now = datetime.now(UTC)
        _freeze_now(monkeypatch, now)
        family = await self._seed_stale_family(
            session,
            max_sequence=5,
            rotated_at=now - timedelta(seconds=_GRACE_SECONDS + 10),
        )

        _, theft_detected, grace_replay = await advance_sequence(
            session,
            family.family_id,
            4,
            _ACCOUNT_A,
            reuse_grace_seconds=_GRACE_SECONDS,
            reuse_grace_max_steps=_GRACE_MAX_STEPS,
            reuse_grace_max_per_window=_GRACE_MAX_PER_WINDOW,
        )

        assert theft_detected is True
        assert grace_replay is False
        assert family.is_blacklisted is True

    async def test_reuse_with_never_rotated_family_blacklists(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        now = datetime.now(UTC)
        _freeze_now(monkeypatch, now)
        family = await _seed_family(session, account_id=_ACCOUNT_A, org_id=_ORG_A)
        family.max_sequence = 5

        _, theft_detected, grace_replay = await advance_sequence(
            session,
            family.family_id,
            4,
            _ACCOUNT_A,
            reuse_grace_seconds=_GRACE_SECONDS,
            reuse_grace_max_steps=_GRACE_MAX_STEPS,
            reuse_grace_max_per_window=_GRACE_MAX_PER_WINDOW,
        )

        assert theft_detected is True
        assert grace_replay is False
        assert family.is_blacklisted is True

    async def test_reuse_with_naive_rotated_at_normalises_window(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # SQLite round-trips timezone-aware timestamps as naive datetimes, so
        # _as_utc_aware must normalise the stored rotated_at before the window
        # interval arithmetic (otherwise the subtraction raises TypeError).
        now = datetime.now(UTC)
        _freeze_now(monkeypatch, now)
        family = await self._seed_stale_family(
            session,
            max_sequence=5,
            rotated_at=(now - timedelta(seconds=5)).replace(tzinfo=None),
        )

        new_sequence, theft_detected, grace_replay = await advance_sequence(
            session,
            family.family_id,
            4,
            _ACCOUNT_A,
            reuse_grace_seconds=_GRACE_SECONDS,
            reuse_grace_max_steps=_GRACE_MAX_STEPS,
            reuse_grace_max_per_window=_GRACE_MAX_PER_WINDOW,
        )

        assert theft_detected is False
        assert grace_replay is True
        assert new_sequence == 6

    async def test_reuse_exceeding_per_window_budget_blacklists(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        now = datetime.now(UTC)
        _freeze_now(monkeypatch, now)
        family = await self._seed_stale_family(
            session,
            max_sequence=5,
            rotated_at=now - timedelta(seconds=5),
            reuse_window_started_at=now - timedelta(seconds=5),
            reuse_replay_count=_GRACE_MAX_PER_WINDOW,
        )

        _, theft_detected, grace_replay = await advance_sequence(
            session,
            family.family_id,
            4,
            _ACCOUNT_A,
            reuse_grace_seconds=_GRACE_SECONDS,
            reuse_grace_max_steps=_GRACE_MAX_STEPS,
            reuse_grace_max_per_window=_GRACE_MAX_PER_WINDOW,
        )

        assert theft_detected is True
        assert grace_replay is False
        assert family.is_blacklisted is True

    async def test_reuse_starts_new_window_after_expiry_reset(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        now = datetime.now(UTC)
        _freeze_now(monkeypatch, now)
        family = await self._seed_stale_family(
            session,
            max_sequence=5,
            rotated_at=now - timedelta(seconds=5),
            reuse_window_started_at=now - timedelta(seconds=_GRACE_SECONDS + 60),
            reuse_replay_count=_GRACE_MAX_PER_WINDOW,
        )

        new_sequence, theft_detected, grace_replay = await advance_sequence(
            session,
            family.family_id,
            4,
            _ACCOUNT_A,
            reuse_grace_seconds=_GRACE_SECONDS,
            reuse_grace_max_steps=_GRACE_MAX_STEPS,
            reuse_grace_max_per_window=_GRACE_MAX_PER_WINDOW,
        )

        assert theft_detected is False
        assert grace_replay is True
        assert new_sequence == 6
        assert family.reuse_replay_count == 1
        assert family.reuse_window_started_at is not None
