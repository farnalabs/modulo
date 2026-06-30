"""Unit tests for payload cleanup job."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from modulo.core.cleanup_jobs.payload_cleanup import (
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


class TestCleanupRetainedPayloads:
    async def test_updates_old_run_payloads(self, mock_session: AsyncMock) -> None:
        result = MagicMock()
        result.rowcount = 5
        mock_session.execute = AsyncMock(return_value=result)

        count = await cleanup_retained_payloads(mock_session)

        assert count == 5

    async def test_skips_runs_within_retention(self, mock_session: AsyncMock) -> None:
        result = MagicMock()
        result.rowcount = 0
        mock_session.execute = AsyncMock(return_value=result)

        count = await cleanup_retained_payloads(mock_session)

        assert count == 0

    async def test_uses_custom_retention_days(self, mock_session: AsyncMock) -> None:
        result = MagicMock()
        result.rowcount = 3
        mock_session.execute = AsyncMock(return_value=result)

        await cleanup_retained_payloads(mock_session, retention_days=60)

        cutoff = datetime.now(UTC) - timedelta(days=60)
        assert cutoff < datetime.now(UTC)

    async def test_sets_payload_columns_to_null(self, mock_session: AsyncMock) -> None:
        result = MagicMock()
        result.rowcount = 2
        mock_session.execute = AsyncMock(return_value=result)

        await cleanup_retained_payloads(mock_session)

        stmt = mock_session.execute.call_args[0][0]
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))

        assert "input_payload" in compiled
        assert "outputs_json" in compiled

    async def test_commits_transaction(self, mock_session: AsyncMock) -> None:
        result = MagicMock()
        result.rowcount = 2
        mock_session.execute = AsyncMock(return_value=result)

        await cleanup_retained_payloads(mock_session)

        mock_session.commit.assert_awaited_once()

    async def test_returns_zero_when_no_runs(self, mock_session: AsyncMock) -> None:
        result = MagicMock()
        result.rowcount = 0
        mock_session.execute = AsyncMock(return_value=result)

        count = await cleanup_retained_payloads(mock_session)

        assert count == 0
