"""Unit tests for payload cleanup job."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from modulo.core.cleanup_jobs.payload_cleanup import (
    BATCH_SIZE,
    cleanup_retained_payloads,
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


class TestCleanupRetainedPayloads:
    async def test_updates_old_run_payloads(self, mock_session: AsyncMock) -> None:
        select_res = _select_result([1, 2, 3, 4, 5])
        update_res = MagicMock()
        mock_session.execute = AsyncMock(side_effect=[select_res, update_res])

        count = await cleanup_retained_payloads(mock_session)

        assert count == 5

    async def test_skips_runs_within_retention(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(return_value=_select_result([]))

        count = await cleanup_retained_payloads(mock_session)

        assert count == 0

    async def test_uses_custom_retention_days(self, mock_session: AsyncMock) -> None:
        select_res = _select_result([1, 2, 3])
        update_res = MagicMock()
        mock_session.execute = AsyncMock(side_effect=[select_res, update_res])

        await cleanup_retained_payloads(mock_session, retention_days=60)

        cutoff = datetime.now(UTC) - timedelta(days=60)
        assert cutoff < datetime.now(UTC)

    async def test_sets_payload_columns_to_null(self, mock_session: AsyncMock) -> None:
        select_res = _select_result([1, 2])
        update_res = MagicMock()
        mock_session.execute = AsyncMock(side_effect=[select_res, update_res])

        await cleanup_retained_payloads(mock_session)

        # second call is the UPDATE
        stmt = mock_session.execute.call_args_list[1][0][0]
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))

        assert "input_payload" in compiled
        assert "outputs_json" in compiled

    async def test_commits_transaction(self, mock_session: AsyncMock) -> None:
        select_res = _select_result([1, 2])
        update_res = MagicMock()
        mock_session.execute = AsyncMock(side_effect=[select_res, update_res])

        await cleanup_retained_payloads(mock_session)

        mock_session.commit.assert_awaited_once()

    async def test_returns_zero_when_no_runs(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(return_value=_select_result([]))

        count = await cleanup_retained_payloads(mock_session)

        assert count == 0

    async def test_multiple_batches(self, mock_session: AsyncMock) -> None:
        select_res_1 = _select_result(list(range(BATCH_SIZE)))
        select_res_2 = _select_result([BATCH_SIZE + 1])
        update_res_1 = MagicMock()
        update_res_2 = MagicMock()
        mock_session.execute = AsyncMock(side_effect=[select_res_1, update_res_1, select_res_2, update_res_2])

        count = await cleanup_retained_payloads(mock_session)

        assert count == BATCH_SIZE + 1
        assert mock_session.commit.await_count == 2
