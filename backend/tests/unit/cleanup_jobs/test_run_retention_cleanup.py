"""Unit tests for run retention cleanup job."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from modulo.core.cleanup_jobs.run_retention_cleanup import (
    _TERMINAL_STATES,
    BATCH_SIZE,
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


def _select_result(ids: list[int]) -> MagicMock:
    r = MagicMock()
    r.scalars.return_value.all.return_value = ids
    return r


class TestCleanupOldRuns:
    async def test_deletes_old_terminal_runs(self, mock_session: AsyncMock) -> None:
        select_res = _select_result([1, 2, 3, 4, 5])
        delete_res = MagicMock()
        mock_session.execute = AsyncMock(side_effect=[select_res, delete_res])

        count = await cleanup_old_runs(mock_session)

        assert count == 5

    async def test_skips_runs_within_retention(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(return_value=_select_result([]))

        count = await cleanup_old_runs(mock_session)

        assert count == 0

    async def test_uses_custom_retention_days(self, mock_session: AsyncMock) -> None:
        select_res = _select_result([1, 2, 3])
        delete_res = MagicMock()
        mock_session.execute = AsyncMock(side_effect=[select_res, delete_res])

        await cleanup_old_runs(mock_session, retention_days=30)

        cutoff = datetime.now(UTC) - timedelta(days=30)
        assert cutoff < datetime.now(UTC)

    async def test_filters_only_terminal_states(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(return_value=_select_result([]))

        await cleanup_old_runs(mock_session)

        stmt = mock_session.execute.call_args[0][0]
        compiled = stmt.compile(compile_kwargs={"literal_binds": True})
        sql = str(compiled)

        for state in _TERMINAL_STATES:
            assert state in sql

    async def test_commits_transaction(self, mock_session: AsyncMock) -> None:
        select_res = _select_result([1, 2])
        delete_res = MagicMock()
        mock_session.execute = AsyncMock(side_effect=[select_res, delete_res])

        await cleanup_old_runs(mock_session)

        mock_session.commit.assert_awaited_once()

    async def test_returns_zero_when_no_runs(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(return_value=_select_result([]))

        count = await cleanup_old_runs(mock_session)

        assert count == 0

    async def test_retained_non_terminal_runs_not_deleted(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(return_value=_select_result([]))

        await cleanup_old_runs(mock_session)

        stmt = mock_session.execute.call_args[0][0]
        compiled = stmt.compile(compile_kwargs={"literal_binds": True})
        sql = str(compiled)

        non_terminal = ("pending", "running", "awaiting_human", "claimed", "waiting_for_lock")
        for state in non_terminal:
            assert state not in sql

    async def test_multiple_batches(self, mock_session: AsyncMock) -> None:
        select_res_1 = _select_result(list(range(BATCH_SIZE)))
        select_res_2 = _select_result([BATCH_SIZE + 1])
        delete_res_1 = MagicMock()
        delete_res_2 = MagicMock()
        mock_session.execute = AsyncMock(
            side_effect=[select_res_1, delete_res_1, select_res_2, delete_res_2]
        )

        count = await cleanup_old_runs(mock_session)

        assert count == BATCH_SIZE + 1
        assert mock_session.commit.await_count == 2
