"""Shared helpers for the ``tests/unit/events`` package.

The events package has three modules that spawn fire-and-forget background
tasks and register them in their own module-global set (``event_bus``,
``listeners``, ``notifier._on_sse_broadcast_done``). Tests that trigger those
paths need the same drain guard before asserting on the task's effects, so the
polling logic lives here once instead of being copied per test module.
"""

from __future__ import annotations

import asyncio
from typing import Any


async def drain_background_tasks(
    task_list: set[asyncio.Task[Any]],
    *,
    interval: float = 0.01,
    rounds: int = 200,
) -> None:
    """Wait until ``task_list`` empties, or fail loudly if it does not.

    Fire-and-forget tasks are tracked with a done-callback, so an empty list
    means every spawned task has completed. Rounds are bounded so a stuck
    pending task raises instead of hanging silently.
    """
    for _ in range(rounds):
        if not task_list:
            return
        await asyncio.sleep(interval)
    raise AssertionError("background tasks did not drain")
