"""FAR-1423 — does the absolute node-deadline watchdog actually FIRE?

The watchdog is structurally armed on every path but has never been observed
firing on prod, and analytics cannot settle it: the FAR-690/693 retry hook
re-dispatches instead of terminal-failing, so 0 ``node_deadline_exceeded``
terminalisations does not imply 0 firings.

The mechanism itself is already pinned elsewhere: the direct-watchdog kill
(``test_pipeline_execution.py`` — stalling node, parallel fan-out), the
wrapper-level no-coverage terminal fail (``test_node_deadline_exceeded_fails_
stalled_node``), and the direct ``_fail_overdue_node`` retry consult
(``test_pipeline_execution_watchdog_retry.py``). The hand-off between them
was NOT: no test drove a hung node's deadline kill through the FULL wrapper
with a ``timeout``-covered policy — so nothing proved the watchdog fires AND
re-dispatches end-to-end (the second open question on the ticket, and the
mechanism behind "0 analytics terminalisations").

This one test settles that gap: a hung node dispatched via the WIRED
``on_node_started`` hook, killed by the real watchdog at its deadline, whose
kill consults the REAL shared hook with a ``{on: ["timeout"], max_retries: 2}``
policy — asserting the re-dispatch surface (fenced pending-reset +
``RunRetryPolicyError`` re-raise for SAQ) and that the run is NEVER
terminal-failed. No fake clock: the deadline is armed by the wrapper at
``time.monotonic()`` and the kill fires through production's real
``_await_progress`` bound (a 0.2s node timeout — a wide margin over any
event-loop jitter and the hook's instant point-in-time checks).
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.pipeline_engine.executor import RunRetryPolicyError
from modulo.core.pipeline_execution import run_executor_with_watchdog

# Short-but-safe node timeout: the kill fires ~0.2s after node start while
# staying far above event-loop jitter (the >=6x margin rule). Production runs
# 1200s; the timing scale does not change the mechanism (the deadline wait is
# production's own ``_await_progress`` bound, not a test sleep).
_NODE_TIMEOUT_SECONDS = 0.2


def _watchdog_retry_executor() -> MagicMock:
    """Executor double exposing exactly the seams the shared hook reuses."""
    executor = MagicMock()
    executor._claim_token = "tok-far-1423"
    executor._read_retry_attempt_state = AsyncMock(return_value=(1, "tok-far-1423"))
    executor._probe_script_lease = AsyncMock(return_value=True)
    executor._fenced_pending_reset = AsyncMock(return_value=1)
    return executor


def _retry_db_seams(retry_policy: dict[str, Any]):
    """Patch the DB seams the shared hook's context loader resolves lazily
    (mirrors ``test_pipeline_execution_watchdog_retry._db_seams``)."""
    run = MagicMock()
    run.trigger_type = "manual"
    run.pipeline_id = uuid.uuid4()
    run.snapshot_id = uuid.uuid4()
    run.claim_count = 1
    pipeline = MagicMock()
    pipeline.retry_policy = retry_policy

    @asynccontextmanager
    async def _session_ctx() -> AsyncIterator[MagicMock]:
        snapshot = MagicMock()
        snapshot.graph_json = {"nodes": [{"id": "n1"}], "edges": []}
        result = MagicMock()
        result.scalar_one_or_none.return_value = snapshot
        session = MagicMock()
        session.execute = AsyncMock(return_value=result)
        begin_cm = AsyncMock()
        begin_cm.__aenter__ = AsyncMock(return_value=None)
        begin_cm.__aexit__ = AsyncMock(return_value=False)
        session.begin = MagicMock(return_value=begin_cm)
        yield session

    factory = MagicMock(side_effect=lambda: _session_ctx())
    return (
        patch("modulo.db.crud.run.get_run", AsyncMock(return_value=run)),
        patch("modulo.db.crud.pipeline.get_pipeline", AsyncMock(return_value=pipeline)),
        patch("modulo.db.rls.set_rls_org", AsyncMock()),
        patch("modulo.db.rls.set_rls_execution_context", AsyncMock()),
        patch("modulo.core.pipeline_engine.watchdog_retry.async_sessionmaker", return_value=factory),
        # The hook's discoverable backoff sleep (45-300s defaults) is patched at
        # the asyncio boundary so the re-dispatch path completes instantly
        # without changing the decision logic under test.
        patch("modulo.core.pipeline_engine.watchdog_retry.asyncio.sleep", new=AsyncMock()),
    )


@pytest.mark.asyncio
async def test_node_deadline_watchdog_fires_and_redispatches_under_timeout_policy() -> None:
    """A hung node killed by the real watchdog at its deadline RE-DISPATCHES
    under a timeout-covered retry policy instead of terminal-failing.

    Prove-the-fix: without the wrapper's hook wiring, the kill would
    terminal-fail with ``node_deadline_exceeded`` — so this test fails if the
    hook is not consulted on the node-deadline path. It also answers "does the
    watchdog fire": the deadline kill must reach the hook (a never-firing
    watchdog raises nothing and the pytest.raises assertion fails).
    """
    executor = _watchdog_retry_executor()
    executor._node_timeouts = {"n1": _NODE_TIMEOUT_SECONDS}
    hang = asyncio.Event()

    async def _hang() -> None:
        # Mimic the real streamed events: the node dispatches (arming the
        # watchdog's absolute deadline via the wired callbacks), then never
        # completes. Hangs on a never-set event — no asyncio.sleep.
        executor.on_first_progress()
        executor.on_node_started("n1")
        await hang.wait()

    fail = AsyncMock(return_value=True)
    with contextlib.ExitStack() as stack:
        for seam in _retry_db_seams({"on": ["timeout"], "max_retries": 2}):
            stack.enter_context(seam)
        stack.enter_context(patch("modulo.core.pipeline_execution.fail_run_terminal", fail))
        stack.enter_context(patch("modulo.core.pipeline_execution.heartbeat_loop", AsyncMock()))
        stack.enter_context(
            patch(
                "modulo.core.pipeline_execution.get_settings",
                lambda: MagicMock(saq_setup_grace_seconds=60, saq_node_default_timeout_seconds=1200),
            )
        )
        with pytest.raises(RunRetryPolicyError) as exc_info:
            await run_executor_with_watchdog(
                MagicMock(),
                run_id=str(uuid.uuid4()),
                org_id=str(uuid.uuid4()),
                executor=executor,
                job=MagicMock(function="execute_run"),
                execute_fn=_hang,
            )
    # The hook re-dispatched: fenced pending-reset ran, RunRetryPolicyError
    # carries the final status / budget SAQ needs, and NO terminal-fail
    # happened (so analytics see 0 terminalisations even though the watchdog
    # fired).
    executor._fenced_pending_reset.assert_awaited_once()
    fail.assert_not_awaited()
    assert exc_info.value.status == "failed"
    assert exc_info.value.max_retries == 2
