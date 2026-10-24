"""Unit tests for ``modulo.db.crud.run_attempts`` (FAR-2078, storage slice).

Behaviour is exercised against a REAL file-backed SQLite database, not mocks:
the ``attempt_index`` contract (0-based, per run, contiguous, unique) and the
close-latest-open-row semantics are SQL semantics, and a mock would only prove
that a mock was built. The Postgres-only half — the per-run advisory lock that
makes concurrent allocation race-safe — is pinned separately at the statement
seam (SQLite has no advisory locks).

The real-Postgres guarantees (concurrent allocation serialised by
``pg_advisory_xact_lock``, RLS isolation, the CHECK constraints as the driver
enforces them) cannot run on this host — Docker/testcontainers are unavailable —
so they are CI-only. What is proven here: the same DDL's CHECK/UNIQUE
constraints hold on a real database, the cascade-on-run-delete that implements
retention, and the exact advisory-lock statement the Postgres path issues.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from modulo.db.crud import run_attempts as crud
from modulo.db.crud.run_attempts import (
    RunAttemptRunNotFoundError,
    close_attempt,
    list_attempts,
    start_attempt,
)
from modulo.db.models.run_attempt import ATTEMPT_TERMINAL_REASON_VALUES, RunAttempt

_ORG = uuid.UUID("00000000-0000-0000-0000-000000000001")
_OTHER_ORG = uuid.UUID("00000000-0000-0000-0000-000000000002")
_RUN = uuid.UUID("11111111-1111-1111-1111-111111111111")
_OTHER_RUN = uuid.UUID("22222222-2222-2222-2222-222222222222")


@pytest.fixture
async def maker(tmp_path: Any) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """File-backed SQLite with the minimal parent tables + the real model DDL."""
    engine: AsyncEngine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'run_attempts.db'}",
        connect_args={"timeout": 10},
    )

    # Enforce FKs so the cascade-on-run-delete retention claim is exercised
    # against the DDL the model declares, not merely asserted in prose.
    @event.listens_for(engine.sync_engine, "connect")
    def _enable_foreign_keys(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.close()

    async with engine.begin() as conn:
        await conn.exec_driver_sql("CREATE TABLE organisations (id VARCHAR(32) PRIMARY KEY)")
        await conn.exec_driver_sql(
            "CREATE TABLE runs (id VARCHAR(32) PRIMARY KEY, organisation_id VARCHAR(32) NOT NULL)"
        )
        await conn.run_sync(RunAttempt.__table__.create)
        await conn.exec_driver_sql(
            "INSERT INTO organisations (id) VALUES (:org), (:other_org)",
            {"org": _ORG.hex, "other_org": _OTHER_ORG.hex},
        )
        await conn.exec_driver_sql(
            "INSERT INTO runs (id, organisation_id) VALUES (:run, :org), (:other_run, :other_org)",
            {"run": _RUN.hex, "org": _ORG.hex, "other_run": _OTHER_RUN.hex, "other_org": _OTHER_ORG.hex},
        )

    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield session_maker
    finally:
        await engine.dispose()


async def _start(
    maker: async_sessionmaker[AsyncSession],
    run_id: uuid.UUID = _RUN,
    **kwargs: Any,
) -> int:
    async with maker() as session, session.begin():
        return await start_attempt(session, run_id, **kwargs)


async def _close(
    maker: async_sessionmaker[AsyncSession],
    run_id: uuid.UUID = _RUN,
    *,
    terminal_reason: str = "completed",
    **kwargs: Any,
) -> int | None:
    async with maker() as session, session.begin():
        return await close_attempt(session, run_id, terminal_reason=terminal_reason, **kwargs)


async def _attempts(maker: async_sessionmaker[AsyncSession], run_id: uuid.UUID = _RUN) -> list[RunAttempt]:
    async with maker() as session:
        return list(await list_attempts(session, run_id))


class TestAttemptIndexContract:
    """The 0-based, per-run, contiguous, monotonic contract the executor relies on."""

    async def test_first_attempt_is_index_zero(self, maker: async_sessionmaker[AsyncSession]) -> None:
        assert await _start(maker) == 0

    async def test_indices_are_contiguous_and_zero_based(self, maker: async_sessionmaker[AsyncSession]) -> None:
        assert [await _start(maker) for _ in range(3)] == [0, 1, 2]

    async def test_indices_are_per_run(self, maker: async_sessionmaker[AsyncSession]) -> None:
        first = await _start(maker, _RUN)
        other = await _start(maker, _OTHER_RUN)
        second = await _start(maker, _RUN)

        assert (first, other, second) == (0, 0, 1)

    async def test_index_is_never_reused_after_close(self, maker: async_sessionmaker[AsyncSession]) -> None:
        assert await _start(maker) == 0
        assert await _close(maker) == 0
        # Closing does not release the index: the next attempt is 1, not 0.
        assert await _start(maker) == 1


class TestStartAttempt:
    async def test_persists_an_open_row_with_the_run_organisation(
        self, maker: async_sessionmaker[AsyncSession]
    ) -> None:
        await _start(maker, model_used="deepseek-v4.1-flash")
        [attempt] = await _attempts(maker)

        assert attempt.organisation_id == _ORG
        assert attempt.run_id == _RUN
        assert attempt.attempt_index == 0
        assert attempt.ended_at is None
        assert attempt.terminal_reason is None
        assert attempt.model_used == "deepseek-v4.1-flash"
        assert attempt.started_at is not None

    async def test_organisation_is_derived_from_the_run_not_the_caller(
        self, maker: async_sessionmaker[AsyncSession]
    ) -> None:
        # _OTHER_RUN belongs to _OTHER_ORG; the row must carry _OTHER_ORG.
        await _start(maker, _OTHER_RUN)
        async with maker() as session:
            [attempt] = list(await list_attempts(session, _OTHER_RUN))
        assert attempt.organisation_id == _OTHER_ORG

    async def test_uses_the_supplied_started_at(self, maker: async_sessionmaker[AsyncSession]) -> None:
        started = datetime(2026, 10, 22, 9, 30, tzinfo=UTC)
        await _start(maker, started_at=started)

        [attempt] = await _attempts(maker)
        assert attempt.started_at.replace(tzinfo=UTC) == started

    async def test_defaults_started_at_to_now(self, maker: async_sessionmaker[AsyncSession]) -> None:
        before = datetime.now(UTC)
        await _start(maker)
        [attempt] = await _attempts(maker)

        stored = attempt.started_at.replace(tzinfo=UTC)
        assert stored >= before - timedelta(seconds=5)

    async def test_unknown_run_raises_a_typed_error(self, maker: async_sessionmaker[AsyncSession]) -> None:
        with pytest.raises(RunAttemptRunNotFoundError, match="not found"):
            await _start(maker, uuid.uuid4())


class TestCloseAttempt:
    async def test_closes_the_latest_open_row_only(self, maker: async_sessionmaker[AsyncSession]) -> None:
        await _start(maker)
        await _start(maker)

        assert await _close(maker, terminal_reason="stall", error_code="node_stalled") == 1

        attempts = {attempt.attempt_index: attempt for attempt in await _attempts(maker)}
        assert attempts[0].ended_at is None
        assert attempts[0].terminal_reason is None
        assert attempts[1].terminal_reason == "stall"
        assert attempts[1].error_code == "node_stalled"
        assert attempts[1].ended_at is not None

    async def test_second_close_closes_the_next_open_row(self, maker: async_sessionmaker[AsyncSession]) -> None:
        await _start(maker)
        await _start(maker)

        assert await _close(maker, terminal_reason="stall") == 1
        assert await _close(maker, terminal_reason="completed") == 0

    async def test_close_with_no_open_row_is_an_idempotent_no_op(self, maker: async_sessionmaker[AsyncSession]) -> None:
        assert await _close(maker) is None

        await _start(maker)
        assert await _close(maker) == 0
        # Re-closing the already-closed run must not touch the row again.
        assert await _close(maker, terminal_reason="reset") is None

    async def test_close_uses_the_supplied_ended_at(self, maker: async_sessionmaker[AsyncSession]) -> None:
        started = datetime(2026, 10, 22, 9, 0, tzinfo=UTC)
        ended = started + timedelta(minutes=7)
        await _start(maker, started_at=started)
        await _close(maker, ended_at=ended)

        [attempt] = await _attempts(maker)
        assert attempt.ended_at.replace(tzinfo=UTC) == ended

    async def test_unknown_terminal_reason_is_rejected_before_the_write(
        self, maker: async_sessionmaker[AsyncSession]
    ) -> None:
        await _start(maker)
        with pytest.raises(ValueError, match="unknown attempt terminal_reason"):
            await _close(maker, terminal_reason="exploded")

        [attempt] = await _attempts(maker)
        assert attempt.ended_at is None
        assert attempt.terminal_reason is None

    @pytest.mark.parametrize("reason", sorted(ATTEMPT_TERMINAL_REASON_VALUES))
    async def test_every_vocabulary_value_is_accepted(
        self, maker: async_sessionmaker[AsyncSession], reason: str
    ) -> None:
        await _start(maker)
        assert await _close(maker, terminal_reason=reason) == 0
        [attempt] = await _attempts(maker)
        assert attempt.terminal_reason == reason


class TestListAttempts:
    async def test_orders_by_attempt_index(self, maker: async_sessionmaker[AsyncSession]) -> None:
        for _ in range(3):
            await _start(maker)
        assert [attempt.attempt_index for attempt in await _attempts(maker)] == [0, 1, 2]

    async def test_scopes_to_the_requested_run(self, maker: async_sessionmaker[AsyncSession]) -> None:
        await _start(maker, _RUN)
        await _start(maker, _OTHER_RUN)

        assert [attempt.run_id for attempt in await _attempts(maker, _RUN)] == [_RUN]


class TestDatabaseConstraints:
    """The CHECK/UNIQUE constraints are the invariant backstop under the helper."""

    async def test_duplicate_attempt_index_for_a_run_is_rejected(self, maker: async_sessionmaker[AsyncSession]) -> None:
        await _start(maker)
        async with maker() as session:
            session.add(
                RunAttempt(
                    organisation_id=_ORG,
                    run_id=_RUN,
                    attempt_index=0,
                    started_at=datetime.now(UTC),
                )
            )
            with pytest.raises(IntegrityError):
                await session.flush()

    async def test_negative_attempt_index_is_rejected(self, maker: async_sessionmaker[AsyncSession]) -> None:
        async with maker() as session:
            with pytest.raises(IntegrityError):
                await session.execute(
                    text(
                        "INSERT INTO run_attempts (id, organisation_id, run_id, attempt_index, started_at) "
                        "VALUES (:id, :org, :run, -1, CURRENT_TIMESTAMP)"
                    ),
                    {"id": uuid.uuid4().hex, "org": _ORG.hex, "run": _RUN.hex},
                )

    async def test_unknown_terminal_reason_is_rejected_by_the_check(
        self, maker: async_sessionmaker[AsyncSession]
    ) -> None:
        async with maker() as session:
            with pytest.raises(IntegrityError):
                await session.execute(
                    text(
                        "INSERT INTO run_attempts "
                        "(id, organisation_id, run_id, attempt_index, started_at, terminal_reason) "
                        "VALUES (:id, :org, :run, 0, CURRENT_TIMESTAMP, 'exploded')"
                    ),
                    {"id": uuid.uuid4().hex, "org": _ORG.hex, "run": _RUN.hex},
                )

    async def test_out_of_order_timestamps_are_retained(self, maker: async_sessionmaker[AsyncSession]) -> None:
        # started_at and ended_at may come from different clocks; the row is
        # diagnostic evidence and must not be rejected for a skew.
        started = datetime(2026, 10, 22, 10, 0, tzinfo=UTC)
        ended = started - timedelta(seconds=1)
        async with maker() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO run_attempts "
                    "(id, organisation_id, run_id, attempt_index, started_at, ended_at) "
                    "VALUES (:id, :org, :run, 0, :started, :ended)"
                ),
                {
                    "id": uuid.uuid4().hex,
                    "org": _ORG.hex,
                    "run": _RUN.hex,
                    "started": started.isoformat(),
                    "ended": ended.isoformat(),
                },
            )

        [attempt] = await _attempts(maker)
        assert attempt.attempt_index == 0


class TestRetentionCascade:
    async def test_deleting_the_run_deletes_its_attempts(self, maker: async_sessionmaker[AsyncSession]) -> None:
        await _start(maker)
        await _start(maker)
        await _start(maker, _OTHER_RUN)

        async with maker() as session, session.begin():
            await session.execute(text("DELETE FROM runs WHERE id = :run"), {"run": _RUN.hex})

        assert not await _attempts(maker, _RUN)
        # The other run's attempt is untouched.
        assert len(await _attempts(maker, _OTHER_RUN)) == 1


def _scalar_result(**kwargs: Any) -> MagicMock:
    result = MagicMock()
    for name, value in kwargs.items():
        getattr(result, name).return_value = value
    return result


def _mock_session_for_start(*, organisation_id: uuid.UUID | None, next_index: int) -> AsyncMock:
    session = AsyncMock(spec=AsyncSession)
    session.execute = AsyncMock(
        side_effect=[
            _scalar_result(scalar_one_or_none=organisation_id),
            MagicMock(),  # advisory lock
            _scalar_result(scalar_one=next_index),
        ]
    )
    return session


class TestPostgresRaceSafety:
    """The advisory lock is what serialises concurrent allocation on Postgres."""

    async def test_postgres_takes_the_per_run_advisory_lock_before_allocating(self) -> None:
        session = _mock_session_for_start(organisation_id=_ORG, next_index=0)
        with patch.object(crud, "get_dialect_name", AsyncMock(return_value="postgresql")):
            index = await start_attempt(session, _RUN)

        assert index == 0
        calls = session.execute.await_args_list
        # 1) derive org, 2) advisory lock, 3) MAX+1 allocation.
        assert len(calls) == 3
        assert "pg_advisory_xact_lock" in str(calls[1].args[0])
        assert calls[1].args[1] == {"key": f"run_attempt:{_RUN}"}
        session.add.assert_called_once()
        session.flush.assert_awaited_once()

    def test_lock_key_is_scoped_per_run(self) -> None:
        assert crud.attempt_lock_key(_RUN) == f"run_attempt:{_RUN}"
        assert crud.attempt_lock_key(_RUN) != crud.attempt_lock_key(_OTHER_RUN)

    async def test_generic_backend_skips_the_advisory_lock(self) -> None:
        session = _mock_session_for_start(organisation_id=_ORG, next_index=2)
        session.execute = AsyncMock(
            side_effect=[
                _scalar_result(scalar_one_or_none=_ORG),
                _scalar_result(scalar_one=2),
            ]
        )
        with patch.object(crud, "get_dialect_name", AsyncMock(return_value="sqlite")):
            index = await start_attempt(session, _RUN)

        assert index == 2
        statements = [str(call.args[0]) for call in session.execute.await_args_list]
        assert not any("pg_advisory_xact_lock" in statement for statement in statements)
