"""Shared mock helpers for the notifier unit tests.

The ``sessionmaker``-shaped factory mock and the ``execute()`` scalar-result
mock were re-implemented in every notifier test module. They live here so a
single definition serves the whole package and the pattern cannot re-grow.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock


def session_factory_for(session: AsyncMock) -> MagicMock:
    """Build a ``sessionmaker``-shaped mock whose ``async with`` yields ``session``.

    Every invocation of the returned factory yields the SAME session, so a code
    path that opens the session more than once (e.g. the owner-read and in-app
    blocks) shares one mock.
    """
    return MagicMock(
        side_effect=lambda: AsyncMock(
            __aenter__=AsyncMock(return_value=session),
            __aexit__=AsyncMock(return_value=False),
        )
    )


def make_scalar_result(items: list[Any]) -> MagicMock:
    """Build an ``execute()`` result mock whose ``scalars()`` iterates ``items``."""
    result = MagicMock()
    result.scalars.return_value.__iter__ = lambda self: iter(items)
    return result
