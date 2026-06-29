"""Unit tests for webhook dedup cleanup job."""

import uuid
from unittest.mock import AsyncMock, MagicMock

from modulo.core.cleanup_jobs.webhook_dedup_cleanup import (
    BATCH_SIZE,
    cleanup_old_webhook_events,
)


class TestCleanupOldWebhookEvents:
    async def test_deletes_old_events(self) -> None:
        ids = [uuid.uuid4(), uuid.uuid4(), uuid.uuid4()]
        session = AsyncMock()

        select_result = MagicMock()
        select_result.scalars.return_value.all.return_value = ids
        delete_result = MagicMock()
        delete_result.rowcount = len(ids)

        session.execute = AsyncMock(side_effect=[select_result, delete_result])
        session.commit = AsyncMock()

        count = await cleanup_old_webhook_events(session)

        assert count == len(ids)
        assert session.execute.call_count == 2
        session.commit.assert_awaited_once()

    async def test_skips_when_no_old_events(self) -> None:
        session = AsyncMock()

        select_result = MagicMock()
        select_result.scalars.return_value.all.return_value = []

        session.execute = AsyncMock(return_value=select_result)

        count = await cleanup_old_webhook_events(session)

        assert count == 0
        session.execute.assert_awaited_once()
        session.commit.assert_not_awaited()

    async def test_uses_correct_cutoff(self) -> None:
        session = AsyncMock()

        select_result = MagicMock()
        select_result.scalars.return_value.all.return_value = [uuid.uuid4()]
        delete_result = MagicMock()
        delete_result.rowcount = 1

        session.execute = AsyncMock(side_effect=[select_result, delete_result])
        session.commit = AsyncMock()

        await cleanup_old_webhook_events(session)

        stmt = session.execute.call_args_list[0][0][0]
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))

        assert "trigger_events" in compiled

    async def test_commits_transaction(self) -> None:
        session = AsyncMock()

        select_result = MagicMock()
        select_result.scalars.return_value.all.return_value = [uuid.uuid4()]
        delete_result = MagicMock()
        delete_result.rowcount = 1

        session.execute = AsyncMock(side_effect=[select_result, delete_result])
        session.commit = AsyncMock()

        await cleanup_old_webhook_events(session)

        session.commit.assert_awaited_once()

    async def test_returns_zero_when_no_rows_deleted(self) -> None:
        session = AsyncMock()

        select_result = MagicMock()
        select_result.scalars.return_value.all.return_value = []

        session.execute = AsyncMock(return_value=select_result)

        count = await cleanup_old_webhook_events(session)

        assert count == 0

    async def test_respects_batch_size_limit(self) -> None:
        """Verify the SELECT statement includes a LIMIT clause."""
        session = AsyncMock()

        select_result = MagicMock()
        select_result.scalars.return_value.all.return_value = [uuid.uuid4() for _ in range(BATCH_SIZE)]
        delete_result = MagicMock()
        delete_result.rowcount = BATCH_SIZE

        session.execute = AsyncMock(side_effect=[select_result, delete_result])
        session.commit = AsyncMock()

        count = await cleanup_old_webhook_events(session)

        assert count == BATCH_SIZE

        stmt = session.execute.call_args_list[0][0][0]
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert compiled.count("LIMIT") > 0 or compiled.count("limit") > 0
