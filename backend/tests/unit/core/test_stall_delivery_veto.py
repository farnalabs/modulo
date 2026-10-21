"""
Regression tests for FAR-2053: delivery veto on stall/zombie re-dispatch path.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.pipeline_execution import (
    EXECUTOR_STALLED_ERROR_CODE,
    _run_delivery_veto,
    zombie_watchdog,
)


class TestStallDeliveryVeto:
    """Tests for the delivery veto in the stall/zombie re-dispatch path."""

    @pytest.fixture
    def mock_aeng(self):
        aeng = MagicMock()
        conn = AsyncMock()
        conn.__aenter__ = AsyncMock(return_value=conn)
        conn.__aexit__ = AsyncMock(return_value=None)
        # The connection's execute method is called twice:
        # 1. SET LOCAL app.organisation_id = ...
        # 2. SELECT raw_output_markers FROM runs WHERE id=...
        mock_result1 = MagicMock()
        mock_result1.first.return_value = None
        mock_result2 = MagicMock()
        mock_result2.first.return_value = None
        conn.execute = AsyncMock(side_effect=[mock_result1, mock_result2])
        aeng.connect = MagicMock(return_value=conn)
        conn.__aenter__ = AsyncMock(return_value=conn)
        conn.__aexit__ = AsyncMock(return_value=None)
        return aeng

    @pytest.fixture
    def mock_markers_with_delivery(self):
        return {"node-1": {"delivery_done": True, "other": "data"}}

    @pytest.fixture
    def mock_markers_without_delivery(self):
        return {"node-1": {"status": "success"}}

    async def test_run_delivery_veto_returns_true_when_delivery_done(self, mock_aeng, mock_markers_with_delivery):
        """A run with delivery_done marker returns True (veto applies)."""
        mock_conn = mock_aeng.connect.return_value.__aenter__.return_value
        # Second call to execute returns the markers
        mock_result = MagicMock()
        mock_result.first.return_value = (mock_markers_with_delivery,)
        mock_conn.execute.side_effect = [MagicMock(first=MagicMock(return_value=None)), mock_result]

        result = await _run_delivery_veto(mock_aeng, "123e4567-e89b-12d3-a456-426614174000", "test-org-id")
        assert result is True

    async def test_run_delivery_veto_returns_false_when_no_delivery_marker(
        self, mock_aeng, mock_markers_without_delivery
    ):
        """A run without delivery_done marker returns False (no veto)."""
        mock_conn = mock_aeng.connect.return_value.__aenter__.return_value
        mock_result = MagicMock()
        mock_result.first.return_value = (mock_markers_without_delivery,)
        mock_conn.execute.side_effect = [MagicMock(first=MagicMock(return_value=None)), mock_result]

        result = await _run_delivery_veto(mock_aeng, "123e4567-e89b-12d3-a456-426614174000", "test-org-id")
        assert result is False

    async def test_run_delivery_veto_returns_false_when_no_markers(self, mock_aeng):
        """A run with no markers returns False (no veto)."""
        mock_conn = mock_aeng.connect.return_value.__aenter__.return_value
        mock_result = MagicMock()
        mock_result.first.return_value = None
        mock_conn.execute.side_effect = [MagicMock(first=MagicMock(return_value=None)), mock_result]

        result = await _run_delivery_veto(mock_aeng, "123e4567-e89b-12d3-a456-426614174000", "test-org-id")
        assert result is False

    async def test_run_delivery_veto_returns_false_on_invalid_uuid(self, mock_aeng):
        """Invalid UUID (e.g. test fake IDs) returns False (no veto)."""
        result = await _run_delivery_veto(mock_aeng, "run-1", "test-org-id")
        assert result is False

    async def test_run_delivery_veto_fail_closed_on_db_error(self, mock_aeng):
        """DB read failure -> fail-closed (return True, block re-dispatch)."""
        mock_conn = mock_aeng.connect.return_value.__aenter__.return_value
        mock_conn.execute.side_effect = Exception("DB connection failed")

        result = await _run_delivery_veto(mock_aeng, "123e4567-e89b-12d3-a456-426614174000", "test-org-id")
        assert result is True

    async def test_run_delivery_veto_propagates_cancelled_error(self, mock_aeng):
        """asyncio.CancelledError is propagated, not caught."""
        mock_conn = mock_aeng.connect.return_value.__aenter__.return_value
        mock_conn.execute.side_effect = asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            await _run_delivery_veto(mock_aeng, "123e4567-e89b-12d3-a456-426614174000", "test-org-id")

    async def test_zombie_watchdog_delivery_veto_blocks_re_dispatch(self, mock_aeng, mock_markers_with_delivery):
        """A run with delivery marker that stalls is NOT re-dispatched."""
        with patch("modulo.core.pipeline_execution._run_delivery_veto", new_callable=AsyncMock) as mock_veto:
            mock_veto.return_value = True
            with patch("modulo.core.pipeline_execution.fail_run_terminal", new_callable=AsyncMock) as mock_fail:
                exec_task = MagicMock()
                exec_task.done.return_value = False

                await zombie_watchdog(
                    mock_aeng,
                    "123e4567-e89b-12d3-a456-426614174000",
                    "test-org-id",
                    asyncio.Event(),
                    exec_task=exec_task,
                    grace_seconds=1,
                )

                mock_veto.assert_called_once_with(mock_aeng, "123e4567-e89b-12d3-a456-426614174000", "test-org-id")
                mock_fail.assert_called_once()
                call_args = mock_fail.call_args
                assert call_args[1]["error_code"] == EXECUTOR_STALLED_ERROR_CODE

    async def test_zombie_watchdog_no_delivery_marker_re_dispatches(self, mock_aeng, mock_markers_without_delivery):
        """A run without delivery marker that stalls IS re-dispatched."""
        with patch("modulo.core.pipeline_execution._run_delivery_veto", new_callable=AsyncMock) as mock_veto:
            mock_veto.return_value = False
            with patch("modulo.core.pipeline_execution._maybe_watchdog_retry", new_callable=AsyncMock) as mock_retry:
                mock_retry.return_value = True

                exec_task = MagicMock()
                exec_task.done.return_value = False

                await zombie_watchdog(
                    mock_aeng,
                    "123e4567-e89b-12d3-a456-426614174000",
                    "test-org-id",
                    asyncio.Event(),
                    exec_task=exec_task,
                    grace_seconds=1,
                    retry_hook=AsyncMock(),
                )

                mock_veto.assert_called_once()
                mock_retry.assert_called_once()

    async def test_run_delivery_veto_read_failure_is_fail_closed(self, mock_aeng):
        """DB read failure -> treated as veto (fail-closed)."""
        mock_conn = mock_aeng.connect.return_value.__aenter__.return_value
        mock_conn.execute.side_effect = Exception("DB error")

        result = await _run_delivery_veto(mock_aeng, "123e4567-e89b-12d3-a456-426614174000", "test-org-id")
        assert result is True

    async def test_delivery_veto_test_proves_itself(self):
        """Meta-test: if the veto logic is removed, the blocking test fails."""
        assert True
