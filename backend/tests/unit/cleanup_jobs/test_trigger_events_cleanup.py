"""Unit tests for the trigger_events age-based retention cleanup (FAR-167).

Covers ``cleanup_old_trigger_events`` (the core batch function), the
in-process scheduler loop, and the SAQ cron wrapper in
``modulo.core.saq_worker`` — including the autobegin=False transaction
convention that the system session factory requires.
"""

import asyncio
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import modulo.core.saq_worker as sw
from modulo.core.cleanup_jobs.trigger_events_cleanup import (
    _CLEANUP_INTERVAL_SECONDS,
    BATCH_SIZE,
    DEFAULT_RETENTION_DAYS,
    cleanup_old_trigger_events,
    cleanup_scheduler_loop,
)
from modulo.db.models.base import Base
from modulo.db.models.trigger_event import TriggerEvent


def _make_session(ids: list[uuid.UUID] | None = None) -> AsyncMock:
    """Return a mock async session pre-wired for a single cleanup pass."""
    session = AsyncMock()
    select_result = MagicMock()
    select_result.scalars.return_value.all.return_value = ids or []
    if ids:
        delete_result = MagicMock()
        delete_result.rowcount = len(ids)
        session.execute = AsyncMock(side_effect=[select_result, delete_result])
    else:
        session.execute = AsyncMock(return_value=select_result)
    session.commit = AsyncMock()
    return session


def _make_factory(session: AsyncMock) -> MagicMock:
    """Return a mock ``async_sessionmaker`` yielding *session* via ``async with``."""
    factory = MagicMock()
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=session)
    factory.return_value = context
    return factory


class TestCleanupOldTriggerEvents:
    async def test_deletes_old_events(self) -> None:
        ids = [uuid.uuid4(), uuid.uuid4(), uuid.uuid4()]
        session = _make_session(ids)

        count = await cleanup_old_trigger_events(session)

        assert count == len(ids)
        assert session.execute.await_count == 2
        session.commit.assert_awaited_once()

    async def test_skips_when_no_old_events(self) -> None:
        session = _make_session()

        count = await cleanup_old_trigger_events(session)

        assert count == 0
        session.execute.assert_awaited_once()
        session.commit.assert_not_awaited()

    async def test_filters_on_received_at(self) -> None:
        """SELECT must filter ``received_at`` strictly below now - retention_days."""
        session = _make_session([uuid.uuid4()])
        retention_days = 7

        await cleanup_old_trigger_events(session, retention_days=retention_days)

        stmt = session.execute.call_args_list[0][0][0]
        assert "received_at" in str(stmt)
        cutoff = next(v for v in stmt.compile().params.values() if isinstance(v, datetime))
        expected = datetime.now(UTC) - timedelta(days=retention_days)
        assert abs((cutoff - expected).total_seconds()) < 5

    async def test_uses_default_retention_when_unspecified(self) -> None:
        session = _make_session([uuid.uuid4()])

        await cleanup_old_trigger_events(session)

        stmt = session.execute.call_args_list[0][0][0]
        cutoff = next(v for v in stmt.compile().params.values() if isinstance(v, datetime))
        expected = datetime.now(UTC) - timedelta(days=DEFAULT_RETENTION_DAYS)
        assert abs((cutoff - expected).total_seconds()) < 5

    @pytest.mark.parametrize("retention_days", [0, -1, -365])
    async def test_rejects_invalid_retention_days(self, retention_days: int) -> None:
        session = _make_session()

        with pytest.raises(ValueError, match="retention_days"):
            await cleanup_old_trigger_events(session, retention_days=retention_days)

        session.execute.assert_not_awaited()

    async def test_respects_batch_size_limit(self) -> None:
        """SELECT must be capped at BATCH_SIZE rows per pass."""
        session = _make_session([uuid.uuid4() for _ in range(BATCH_SIZE)])

        await cleanup_old_trigger_events(session)

        stmt = session.execute.call_args_list[0][0][0]
        limit = next(v for v in stmt.compile().params.values() if isinstance(v, int))
        assert limit == BATCH_SIZE

    async def test_delete_targets_returned_ids(self) -> None:
        ids = [uuid.uuid4(), uuid.uuid4()]
        session = _make_session(ids)

        await cleanup_old_trigger_events(session)

        delete_stmt = session.execute.call_args_list[1][0][0]
        assert delete_stmt.table.name == "trigger_events"
        target_ids = next(v for v in delete_stmt.compile().params.values() if isinstance(v, list))
        assert set(target_ids) == set(ids)

    async def test_commits_transaction(self) -> None:
        session = _make_session([uuid.uuid4()])

        await cleanup_old_trigger_events(session)

        session.commit.assert_awaited_once()

    async def test_commit_failure_propagates(self) -> None:
        session = _make_session([uuid.uuid4()])
        session.commit.side_effect = RuntimeError("db down")

        with pytest.raises(RuntimeError, match="db down"):
            await cleanup_old_trigger_events(session)


class TestCleanupSchedulerLoop:
    async def test_drains_batches_then_resets_backoff(self) -> None:
        factory = _make_factory(AsyncMock())

        with (
            patch(
                "modulo.core.cleanup_jobs.trigger_events_cleanup.cleanup_old_trigger_events",
                side_effect=[BATCH_SIZE, 3, asyncio.CancelledError()],
            ) as mock_cleanup,
            patch(
                "modulo.core.cleanup_jobs.trigger_events_cleanup.asyncio.sleep",
                new_callable=AsyncMock,
            ) as mock_sleep,
            pytest.raises(asyncio.CancelledError),
        ):
            await cleanup_scheduler_loop(factory)

        assert mock_cleanup.await_count == 3
        mock_sleep.assert_awaited_once_with(_CLEANUP_INTERVAL_SECONDS)

    async def test_breaks_on_cancellation(self) -> None:
        factory = _make_factory(AsyncMock())

        with (
            patch(
                "modulo.core.cleanup_jobs.trigger_events_cleanup.cleanup_old_trigger_events",
                side_effect=asyncio.CancelledError(),
            ) as mock_cleanup,
            patch(
                "modulo.core.cleanup_jobs.trigger_events_cleanup.asyncio.sleep",
                new_callable=AsyncMock,
            ) as mock_sleep,
            pytest.raises(asyncio.CancelledError),
        ):
            await cleanup_scheduler_loop(factory)

        mock_cleanup.assert_awaited_once()
        mock_sleep.assert_not_awaited()

    async def test_backs_off_exponentially_after_errors(self) -> None:
        factory = _make_factory(AsyncMock())

        with (
            patch(
                "modulo.core.cleanup_jobs.trigger_events_cleanup.cleanup_old_trigger_events",
                side_effect=[OSError("boom"), OSError("boom"), asyncio.CancelledError()],
            ) as mock_cleanup,
            patch(
                "modulo.core.cleanup_jobs.trigger_events_cleanup.asyncio.sleep",
                new_callable=AsyncMock,
            ) as mock_sleep,
            pytest.raises(asyncio.CancelledError),
        ):
            await cleanup_scheduler_loop(factory)

        assert mock_cleanup.await_count == 3
        assert mock_sleep.await_count == 2
        assert mock_sleep.await_args_list[0].args == (1,)
        assert mock_sleep.await_args_list[1].args == (2,)

    async def test_backoff_caps_at_cleanup_interval(self) -> None:
        factory = _make_factory(AsyncMock())

        errors = [OSError("boom")] * 13 + [asyncio.CancelledError()]
        with (
            patch(
                "modulo.core.cleanup_jobs.trigger_events_cleanup.cleanup_old_trigger_events",
                side_effect=errors,
            ) as mock_cleanup,
            patch(
                "modulo.core.cleanup_jobs.trigger_events_cleanup.asyncio.sleep",
                new_callable=AsyncMock,
            ) as mock_sleep,
            pytest.raises(asyncio.CancelledError),
        ):
            await cleanup_scheduler_loop(factory)

        assert mock_cleanup.await_count == len(errors)
        assert mock_sleep.await_count == 13
        assert mock_sleep.await_args_list[-1].args == (_CLEANUP_INTERVAL_SECONDS,)


class TestSaqTriggerEventsCleanup:
    """The ``trigger_events_cleanup`` job in ``modulo.core.saq_worker`` wraps
    ``cleanup_old_trigger_events`` in a drain loop and reports the total
    deleted count."""

    def _make_factory_with_session(self) -> tuple[MagicMock, MagicMock]:
        """Return a mock sessionmaker (via ``async with``) plus its session.

        ``session`` is a plain MagicMock (not AsyncMock) so ``session.begin()``
        returns the context-manager mock synchronously — the cron opens an
        explicit per-batch transaction.
        """
        session = MagicMock()
        begin_cm = MagicMock()
        begin_cm.__aenter__ = AsyncMock(return_value=None)
        begin_cm.__aexit__ = AsyncMock(return_value=False)
        session.begin.return_value = begin_cm
        factory = MagicMock()
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=session)
        factory.return_value = context
        return factory, session

    async def test_drains_multiple_batches(self) -> None:
        factory, _ = self._make_factory_with_session()

        with (
            patch.object(sw, "_make_session_factory", return_value=factory),
            patch(
                "modulo.core.cleanup_jobs.trigger_events_cleanup.cleanup_old_trigger_events",
                new_callable=AsyncMock,
                side_effect=[BATCH_SIZE, BATCH_SIZE, 3],
            ) as mock_cleanup,
        ):
            result = await sw.trigger_events_cleanup({})

        assert result == {"deleted": BATCH_SIZE * 2 + 3}
        assert mock_cleanup.await_count == 3

    async def test_single_pass_when_below_threshold(self) -> None:
        factory, _ = self._make_factory_with_session()

        with (
            patch.object(sw, "_make_session_factory", return_value=factory),
            patch(
                "modulo.core.cleanup_jobs.trigger_events_cleanup.cleanup_old_trigger_events",
                new_callable=AsyncMock,
                return_value=0,
            ) as mock_cleanup,
        ):
            result = await sw.trigger_events_cleanup({})

        assert result == {"deleted": 0}
        mock_cleanup.assert_awaited_once()

    async def test_propagates_cleanup_error(self) -> None:
        factory, _ = self._make_factory_with_session()

        with (
            patch.object(sw, "_make_session_factory", return_value=factory),
            patch(
                "modulo.core.cleanup_jobs.trigger_events_cleanup.cleanup_old_trigger_events",
                new_callable=AsyncMock,
                side_effect=RuntimeError("db down"),
            ),
            pytest.raises(RuntimeError, match="db down"),
        ):
            await sw.trigger_events_cleanup({})


class TestSaqTriggerEventsCleanupAutobeginTransaction:
    """Regression: the system session factory is ``autobegin=False`` (F1
    convention), so the cron MUST open an explicit per-batch transaction.

    These tests exercise the REAL autobegin=False session factory and the REAL
    ``cleanup_old_trigger_events`` against an in-memory SQLite schema — not a
    mocked session.
    """

    @pytest.fixture
    async def autobegin_false_factory(self) -> AsyncGenerator[async_sessionmaker, None]:
        engine = create_async_engine("sqlite+aiosqlite://", echo=False)
        async with engine.begin() as conn:
            await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=[TriggerEvent.__table__]))
            # The trigger_events FK targets (organisations/triggers/runs) are
            # not created here; foreign-key enforcement is disabled for the test.
            await conn.exec_driver_sql("PRAGMA foreign_keys = OFF")
        factory = async_sessionmaker(engine, expire_on_commit=False, autobegin=False)
        try:
            yield factory
        finally:
            await engine.dispose()

    async def test_cron_deletes_old_rows_against_autobegin_false_session(self, autobegin_false_factory) -> None:
        """Prove-the-fix: an old event survives WITHOUT the cleanup job and is
        deleted WITH it, through the real autobegin=False session factory."""
        old_event = TriggerEvent(
            organisation_id=uuid.uuid4(),
            trigger_id=uuid.uuid4(),
            trigger_type="cron",
            raw_payload_hash="a" * 64,
            received_at=datetime.now(UTC) - timedelta(days=DEFAULT_RETENTION_DAYS + 1),
            validation_result="accepted",
        )
        async with autobegin_false_factory() as session, session.begin():
            session.add(old_event)

        with patch.object(sw, "_make_session_factory", return_value=autobegin_false_factory):
            result = await sw.trigger_events_cleanup({})

        assert result == {"deleted": 1}
        async with autobegin_false_factory() as session, session.begin():
            remaining = list((await session.execute(select(TriggerEvent))).scalars().all())
        assert remaining == []

    async def test_keeps_recent_rows(self, autobegin_false_factory) -> None:
        """Events inside the retention window must NOT be purged."""
        recent_event = TriggerEvent(
            organisation_id=uuid.uuid4(),
            trigger_id=uuid.uuid4(),
            trigger_type="webhook",
            raw_payload_hash="b" * 64,
            received_at=datetime.now(UTC),
            validation_result="accepted",
        )
        async with autobegin_false_factory() as session, session.begin():
            session.add(recent_event)

        with patch.object(sw, "_make_session_factory", return_value=autobegin_false_factory):
            result = await sw.trigger_events_cleanup({})

        assert result == {"deleted": 0}
        async with autobegin_false_factory() as session, session.begin():
            remaining = list((await session.execute(select(TriggerEvent))).scalars().all())
        assert len(remaining) == 1

    async def test_cron_zero_batch_against_autobegin_false_session(self, autobegin_false_factory) -> None:
        """Even an empty table must not raise: the SELECT needs an active
        transaction on an autobegin=False session."""
        with patch.object(sw, "_make_session_factory", return_value=autobegin_false_factory):
            result = await sw.trigger_events_cleanup({})

        assert result == {"deleted": 0}
