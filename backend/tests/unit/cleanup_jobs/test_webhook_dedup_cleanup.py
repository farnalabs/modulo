"""Unit tests for webhook dedup cleanup job."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.cleanup_jobs.webhook_dedup_cleanup import (
    _CLEANUP_INTERVAL_SECONDS,
    BATCH_SIZE,
    DEFAULT_RETENTION_DAYS,
    cleanup_old_webhook_events,
    cleanup_scheduler_loop,
)


def _make_session(ids: list[uuid.UUID] | None = None) -> AsyncMock:
    """Return a mock async session pre-wired for a single cleanup pass.

    The first ``execute`` returns the SELECT result wrapping *ids*; when *ids*
    is non-empty a second ``execute`` returns a DELETE result with a rowcount.
    """
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


class TestCleanupOldWebhookEvents:
    async def test_deletes_old_events(self) -> None:
        ids = [uuid.uuid4(), uuid.uuid4(), uuid.uuid4()]
        session = _make_session(ids)

        count = await cleanup_old_webhook_events(session)

        assert count == len(ids)
        assert session.execute.await_count == 2
        session.commit.assert_awaited_once()

    async def test_skips_when_no_old_events(self) -> None:
        session = _make_session()

        count = await cleanup_old_webhook_events(session)

        assert count == 0
        session.execute.assert_awaited_once()
        session.commit.assert_not_awaited()

    async def test_uses_correct_cutoff(self) -> None:
        """SELECT must filter ``created_at`` strictly below now - retention_days."""
        session = _make_session([uuid.uuid4()])
        retention_days = 7

        await cleanup_old_webhook_events(session, retention_days=retention_days)

        stmt = session.execute.call_args_list[0][0][0]
        cutoff = next(v for v in stmt.compile().params.values() if isinstance(v, datetime))
        expected = datetime.now(UTC) - timedelta(days=retention_days)
        assert abs((cutoff - expected).total_seconds()) < 5

    async def test_uses_default_retention_when_unspecified(self) -> None:
        session = _make_session([uuid.uuid4()])

        await cleanup_old_webhook_events(session)

        stmt = session.execute.call_args_list[0][0][0]
        cutoff = next(v for v in stmt.compile().params.values() if isinstance(v, datetime))
        expected = datetime.now(UTC) - timedelta(days=DEFAULT_RETENTION_DAYS)
        assert abs((cutoff - expected).total_seconds()) < 5

    @pytest.mark.parametrize("retention_days", [0, -1, -365])
    async def test_rejects_invalid_retention_days(self, retention_days: int) -> None:
        session = _make_session()

        with pytest.raises(ValueError, match="retention_days"):
            await cleanup_old_webhook_events(session, retention_days=retention_days)

        session.execute.assert_not_awaited()

    async def test_respects_batch_size_limit(self) -> None:
        """SELECT must be capped at BATCH_SIZE rows per pass."""
        session = _make_session([uuid.uuid4() for _ in range(BATCH_SIZE)])

        await cleanup_old_webhook_events(session)

        stmt = session.execute.call_args_list[0][0][0]
        limit = next(v for v in stmt.compile().params.values() if isinstance(v, int))
        assert limit == BATCH_SIZE

    async def test_delete_targets_returned_ids(self) -> None:
        ids = [uuid.uuid4(), uuid.uuid4()]
        session = _make_session(ids)

        await cleanup_old_webhook_events(session)

        delete_stmt = session.execute.call_args_list[1][0][0]
        assert delete_stmt.table.name == "trigger_events"
        target_ids = next(v for v in delete_stmt.compile().params.values() if isinstance(v, list))
        assert set(target_ids) == set(ids)

    async def test_commits_transaction(self) -> None:
        session = _make_session([uuid.uuid4()])

        await cleanup_old_webhook_events(session)

        session.commit.assert_awaited_once()

    async def test_commit_failure_propagates(self) -> None:
        session = _make_session([uuid.uuid4()])
        session.commit.side_effect = RuntimeError("db down")

        with pytest.raises(RuntimeError, match="db down"):
            await cleanup_old_webhook_events(session)


class TestCleanupSchedulerLoop:
    # ``cleanup_scheduler_loop`` is an infinite ``while True`` loop that only
    # exits on ``asyncio.CancelledError``. To drive it to termination in these
    # tests, the mocked ``cleanup_old_webhook_events`` uses
    # ``asyncio.CancelledError`` as its final side_effect, letting the loop
    # break naturally after the assertions of interest have been observed.

    async def test_drains_batches_then_resets_backoff(self) -> None:
        """Keep deleting until a pass returns fewer than BATCH_SIZE, then sleep."""
        factory = _make_factory(AsyncMock())

        with (
            patch(
                "modulo.core.cleanup_jobs.webhook_dedup_cleanup.cleanup_old_webhook_events",
                side_effect=[BATCH_SIZE, 3, asyncio.CancelledError()],
            ) as mock_cleanup,
            patch(
                "modulo.core.cleanup_jobs.webhook_dedup_cleanup.asyncio.sleep",
                new_callable=AsyncMock,
            ) as mock_sleep,
        ):
            await cleanup_scheduler_loop(factory)

        assert mock_cleanup.await_count == 3
        mock_sleep.assert_awaited_once_with(_CLEANUP_INTERVAL_SECONDS)

    async def test_breaks_on_cancellation(self) -> None:
        factory = _make_factory(AsyncMock())

        with (
            patch(
                "modulo.core.cleanup_jobs.webhook_dedup_cleanup.cleanup_old_webhook_events",
                side_effect=asyncio.CancelledError(),
            ) as mock_cleanup,
            patch(
                "modulo.core.cleanup_jobs.webhook_dedup_cleanup.asyncio.sleep",
                new_callable=AsyncMock,
            ) as mock_sleep,
        ):
            await cleanup_scheduler_loop(factory)

        mock_cleanup.assert_awaited_once()
        mock_sleep.assert_not_awaited()

    async def test_backs_off_exponentially_after_errors(self) -> None:
        factory = _make_factory(AsyncMock())

        with (
            patch(
                "modulo.core.cleanup_jobs.webhook_dedup_cleanup.cleanup_old_webhook_events",
                side_effect=[OSError("boom"), OSError("boom"), asyncio.CancelledError()],
            ) as mock_cleanup,
            patch(
                "modulo.core.cleanup_jobs.webhook_dedup_cleanup.asyncio.sleep",
                new_callable=AsyncMock,
            ) as mock_sleep,
        ):
            await cleanup_scheduler_loop(factory)

        assert mock_cleanup.await_count == 3
        assert mock_sleep.await_count == 2
        assert mock_sleep.await_args_list[0].args == (1,)
        assert mock_sleep.await_args_list[1].args == (2,)

    async def test_backoff_caps_at_cleanup_interval(self) -> None:
        """Exponential backoff must never exceed the cleanup interval."""
        factory = _make_factory(AsyncMock())

        errors = [OSError("boom")] * 13 + [asyncio.CancelledError()]
        with (
            patch(
                "modulo.core.cleanup_jobs.webhook_dedup_cleanup.cleanup_old_webhook_events",
                side_effect=errors,
            ) as mock_cleanup,
            patch(
                "modulo.core.cleanup_jobs.webhook_dedup_cleanup.asyncio.sleep",
                new_callable=AsyncMock,
            ) as mock_sleep,
        ):
            await cleanup_scheduler_loop(factory)

        assert mock_cleanup.await_count == len(errors)
        assert mock_sleep.await_count == 13
        assert mock_sleep.await_args_list[-1].args == (_CLEANUP_INTERVAL_SECONDS,)

    async def test_resets_backoff_after_success_preceded_by_errors(self) -> None:
        """A successful drain must reset backoff to the full interval even after errors."""
        factory = _make_factory(AsyncMock())

        with (
            patch(
                "modulo.core.cleanup_jobs.webhook_dedup_cleanup.cleanup_old_webhook_events",
                side_effect=[OSError("boom"), OSError("boom"), BATCH_SIZE, 3, asyncio.CancelledError()],
            ) as mock_cleanup,
            patch(
                "modulo.core.cleanup_jobs.webhook_dedup_cleanup.asyncio.sleep",
                new_callable=AsyncMock,
            ) as mock_sleep,
        ):
            await cleanup_scheduler_loop(factory)

        assert mock_cleanup.await_count == 5
        assert mock_sleep.await_count == 3
        assert mock_sleep.await_args_list[0].args == (1,)
        assert mock_sleep.await_args_list[1].args == (2,)
        assert mock_sleep.await_args_list[2].args == (_CLEANUP_INTERVAL_SECONDS,)
