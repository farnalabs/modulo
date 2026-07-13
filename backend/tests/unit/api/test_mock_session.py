"""Tests for strict AsyncSession mock configuration."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.unit.api.mock_session import configure_mock_session


async def test_unstubbed_execute_fails_explicitly() -> None:
    session = configure_mock_session(AsyncMock())

    with pytest.raises(AssertionError, match=r"Unexpected session\.execute"):
        await session.execute("SELECT 1")


async def test_empty_execute_result_requires_opt_in() -> None:
    session = configure_mock_session(AsyncMock(), allow_empty_execute=True)

    result = await session.execute("SELECT 1")

    assert result.scalar() == 0
    assert result.scalar_one_or_none() is None
    assert result.scalars().all() == []


async def test_explicit_execute_result_overrides_strict_default() -> None:
    session = configure_mock_session(AsyncMock())
    expected = MagicMock()
    session.execute.return_value = expected

    assert await session.execute("SELECT 1") is expected
