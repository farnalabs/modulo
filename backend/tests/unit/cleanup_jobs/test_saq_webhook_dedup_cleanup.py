"""Unit tests for the SAQ wrapper around webhook dedup cleanup.

The ``webhook_dedup_cleanup`` job in ``modulo.core.saq_worker`` wraps
``cleanup_old_webhook_events`` in a drain loop and reports the total deleted
count. It previously had no coverage — the direct module tests only exercised
``cleanup_old_webhook_events`` and the in-process scheduler loop.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import modulo.core.saq_worker as sw
from modulo.core.cleanup_jobs.webhook_dedup_cleanup import BATCH_SIZE


def _make_factory_with_session() -> tuple[MagicMock, AsyncMock]:
    """Return a mock sessionmaker (via ``async with``) plus its session."""
    session = AsyncMock()
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
