"""Unit tests for the SAQ wrapper around webhook dedup cleanup.

The ``webhook_dedup_cleanup`` job in ``modulo.core.saq_worker`` wraps
``cleanup_old_webhook_events`` in a drain loop and reports the total deleted
count. It previously had no coverage — the direct module tests only exercised
``cleanup_old_webhook_events`` and the in-process scheduler loop.
"""

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import modulo.core.saq_worker as sw
from modulo.core.cleanup_jobs.webhook_dedup_cleanup import BATCH_SIZE
from modulo.db.models.base import Base
from modulo.db.models.trigger_event import TriggerEvent


def _make_factory_with_session() -> tuple[MagicMock, MagicMock]:
    """Return a mock sessionmaker (via ``async with``) plus its session.

    ``session`` is a plain MagicMock (not AsyncMock) so ``session.begin()``
    returns the context-manager mock synchronously — the cron opens an explicit
    per-batch transaction. An AsyncMock's ``begin()`` would return a bare
    coroutine that ``async with`` cannot enter.
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


class TestWebhookDedupCleanup:
    async def test_drains_multiple_batches(self) -> None:
        """Keep deleting until a pass returns fewer than BATCH_SIZE rows."""
        factory, _ = _make_factory_with_session()

        with (
            patch.object(sw, "_make_session_factory", return_value=factory),
            patch(
                "modulo.core.cleanup_jobs.webhook_dedup_cleanup.cleanup_old_webhook_events",
                new_callable=AsyncMock,
                side_effect=[BATCH_SIZE, BATCH_SIZE, 3],
            ) as mock_cleanup,
        ):
            result = await sw.webhook_dedup_cleanup({})

        assert result == {"deleted": BATCH_SIZE * 2 + 3}
        assert mock_cleanup.await_count == 3

    async def test_single_pass_when_below_threshold(self) -> None:
        factory, _ = _make_factory_with_session()

        with (
            patch.object(sw, "_make_session_factory", return_value=factory),
            patch(
                "modulo.core.cleanup_jobs.webhook_dedup_cleanup.cleanup_old_webhook_events",
                new_callable=AsyncMock,
                return_value=0,
            ) as mock_cleanup,
        ):
            result = await sw.webhook_dedup_cleanup({})

        assert result == {"deleted": 0}
        mock_cleanup.assert_awaited_once()

    async def test_propagates_cleanup_error(self) -> None:
        """A DB failure inside the drain loop must propagate to the caller."""
        factory, _ = _make_factory_with_session()

        with (
            patch.object(sw, "_make_session_factory", return_value=factory),
            patch(
                "modulo.core.cleanup_jobs.webhook_dedup_cleanup.cleanup_old_webhook_events",
                new_callable=AsyncMock,
                side_effect=RuntimeError("db down"),
            ),
            pytest.raises(RuntimeError, match="db down"),
        ):
            await sw.webhook_dedup_cleanup({})


class TestWebhookDedupCleanupAutobeginTransaction:
    """Regression: the system session factory is ``autobegin=False`` (F1
    convention), so the cron MUST open an explicit per-batch transaction.

    Without it the first ``session.execute`` inside
    ``cleanup_old_webhook_events`` raises ``InvalidRequestError: Autobegin is
    disabled on this Session`` and the hourly retention cron fails on every
    tick. These tests exercise the REAL autobegin=False session factory and the
    REAL ``cleanup_old_webhook_events`` against an in-memory SQLite schema —
    not a mocked session.
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

    async def test_cron_deletes_against_autobegin_false_session(self, autobegin_false_factory) -> None:
        """The cron must delete old events through the autobegin=False factory
        without raising InvalidRequestError."""
        old_event = TriggerEvent(
            organisation_id=uuid.uuid4(),
            trigger_id=uuid.uuid4(),
            trigger_type="webhook",
            raw_payload_hash="a" * 64,
            # The cleanup SELECT filters on ``created_at`` (not ``received_at``);
            # the server default stamps "now", so set an old value explicitly.
            created_at=datetime.now(UTC) - timedelta(days=60),
            received_at=datetime.now(UTC) - timedelta(days=60),
            validation_result="accepted",
        )
        async with autobegin_false_factory() as session, session.begin():
            session.add(old_event)

        with patch.object(sw, "_make_session_factory", return_value=autobegin_false_factory):
            result = await sw.webhook_dedup_cleanup({})

        assert result == {"deleted": 1}

    async def test_cron_zero_batch_against_autobegin_false_session(self, autobegin_false_factory) -> None:
        """Even an empty table must not raise: the SELECT needs an active
        transaction on an autobegin=False session."""
        with patch.object(sw, "_make_session_factory", return_value=autobegin_false_factory):
            result = await sw.webhook_dedup_cleanup({})

        assert result == {"deleted": 0}
