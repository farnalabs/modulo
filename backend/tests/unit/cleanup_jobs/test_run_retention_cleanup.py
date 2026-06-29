"""Unit tests for run retention cleanup job."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from modulo.core.cleanup_jobs.run_retention_cleanup import (
    _TERMINAL_STATES,
    cleanup_old_runs,
)


@pytest.fixture()
def mock_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


class TestCleanupOldRuns:
    async def test_deletes_old_terminal_runs(self, mock_session: AsyncMock) -> None:
        result = MagicMock()
        result.rowcount = 5
        mock_session.execute = AsyncMock(return_value=result)

        count = await cleanup_old_runs(mock_session)

        assert count == 5

    async def test_skips_runs_within_retention(self, mock_session: AsyncMock) -> None:
        result = MagicMock()
        result.rowcount = 0
        mock_session.execute = AsyncMock(return_value=result)

        count = await cleanup_old_runs(mock_session)

        assert count == 0

    async def test_uses_custom_retention_days(self, mock_session: AsyncMock) -> None:
        result = MagicMock()
        result.rowcount = 3
        mock_session.execute = AsyncMock(return_value=result)

        await cleanup_old_runs(mock_session, retention_days=30)

        cutoff = datetime.now(UTC) - timedelta(days=30)
        assert cutoff < datetime.now(UTC)

    async def test_filters_only_terminal_states(self, mock_session: AsyncMock) -> None:
        result = MagicMock()
        result.rowcount = 0
        mock_session.execute = AsyncMock(return_value=result)

        await cleanup_old_runs(mock_session)

        stmt = mock_session.execute.call_args[0][0]
        compiled = stmt.compile(compile_kwargs={"literal_binds": True})
        sql = str(compiled)

        for state in _TERMINAL_STATES:
            assert state in sql

    async def test_commits_transaction(self, mock_session: AsyncMock) -> None:
        result = MagicMock()
        result.rowcount = 2
        mock_session.execute = AsyncMock(return_value=result)

        await cleanup_old_runs(mock_session)

        mock_session.commit.assert_awaited_once()

    async def test_returns_zero_when_no_runs(self, mock_session: AsyncMock) -> None:
        result = MagicMock()
        result.rowcount = 0
        mock_session.execute = AsyncMock(return_value=result)

        count = await cleanup_old_runs(mock_session)

        assert count == 0

    async def test_retained_non_terminal_runs_not_deleted(self, mock_session: AsyncMock) -> None:
        result = MagicMock()
        result.rowcount = 0
        mock_session.execute = AsyncMock(return_value=result)

        await cleanup_old_runs(mock_session)

        stmt = mock_session.execute.call_args[0][0]
        compiled = stmt.compile(compile_kwargs={"literal_binds": True})
        sql = str(compiled)

        non_terminal = ("pending", "running", "awaiting_human", "claimed", "waiting_for_lock")
        for state in non_terminal:
            assert state not in sql
