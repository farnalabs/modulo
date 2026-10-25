"""The sync→async judge bridge, hardened against a wedged loop (ADR 066 D10).

The pre-ADR bridge ran ``asyncio.run(coro)`` per call on a single-worker
executor. Two failure modes followed:

* a coroutine that never finishes occupies that single worker thread forever,
  so every later judge call queues behind it and fails closed until the
  process restarts (observed on the single-worker bridge);
* ``asyncio.run`` builds a fresh loop per call, so any client cached across
  calls is bound to a dead loop on the next one.

This module replaces the per-call loop with a **persistent loop thread**
(``asyncio.run_coroutine_threadsafe``) and adds a **retire-on-wedge**
guarantee: when a call's timeout cannot cancel the running task (an
uncancellable call — a blocking SDK call inside the coroutine), the loop is
abandoned and the next call gets a fresh loop thread. A hung call can
therefore never wedge a later call.

The *preferred* path is the async one: the two high-volume call sites
(pipeline post-node, suite run) await on their own running loop, so no bridge
and no thread is involved at all. The bridge is the compatibility path for
sync callers.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from collections.abc import Coroutine
from typing import Any

_log = logging.getLogger(__name__)

#: Bound on a bridge call when the caller does not supply one.
DEFAULT_BRIDGE_TIMEOUT_SECONDS = 75.0

#: Grace period after requesting cancellation. A running asyncio task unwinds
#: quickly; one that does NOT is inside an uncancellable blocking call and its
#: loop must be retired so later calls cannot queue behind it.
_CANCEL_GRACE_SECONDS = 0.1


class _CarriedBaseError(Exception):
    """Carrier for a BaseException a Task cannot hand back through a Future.

    ``asyncio`` re-raises ``KeyboardInterrupt``/``SystemExit`` out of the loop
    callback rather than setting them on the task's future. Wrapping them in an
    ordinary Exception lets the future carry the failure verbatim, and the
    caller re-raises the original.
    """

    def __init__(self, exc: BaseException) -> None:
        super().__init__(str(exc))
        self.exc = exc


class _LoopThread:
    """One persistent event loop on a daemon thread."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run,
            name="typed-judge-loop",
            daemon=True,
        )
        self._thread.start()
        self.retired = False

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_forever()
        finally:
            # Close only what is still pending on THIS loop; a wedged loop never
            # reaches here, which is exactly why retire() abandons it instead of
            # joining it.
            with contextlib.suppress(Exception):  # pragma: no cover - best-effort teardown
                self.loop.close()

    async def _carry_base_exceptions(self, coro: Coroutine[Any, Any, Any]) -> Any:
        try:
            return await coro
        # NOSONAR python:S5754 — these are deliberately converted into a carrier
        # and re-raised verbatim by the caller (`run` below): a bare re-raise here
        # would escape the loop callback and kill the loop thread before the
        # future could report the failure, wedging every later call.
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit) as exc:  # NOSONAR
            raise _CarriedBaseError(exc) from None

    def run(self, coro: Coroutine[Any, Any, Any], timeout: float) -> Any:
        """Run *coro* on this loop, cancelling it on timeout when possible."""
        state: dict[str, Any] = {}
        finished = threading.Event()

        async def _tracked() -> Any:
            state["task"] = asyncio.current_task()
            return await coro

        future = asyncio.run_coroutine_threadsafe(self._carry_base_exceptions(_tracked()), self.loop)
        try:
            return future.result(timeout=timeout)
        except _CarriedBaseError as carried:
            raise carried.exc from None
        except TimeoutError:
            self._cancel_or_retire(state, finished)
            raise

    def _cancel_or_retire(self, state: dict[str, Any], finished: threading.Event) -> None:
        """Cancel the task, retiring the loop when it cannot unwind.

        The wrapper ``concurrent.futures.Future`` reports cancellation the
        moment ``cancel()`` is requested, so it cannot prove the coroutine
        stopped. The asyncio task's own done-callback is the proof: if it has
        not fired within the grace period the loop is blocked inside an
        uncancellable call and is abandoned, so a later call gets a fresh loop.
        """
        task = state.get("task")
        if task is None:
            # The task never started: the loop itself is not accepting work.
            retire_loop_thread(self)
            return
        self.loop.call_soon_threadsafe(task.cancel)
        self.loop.call_soon_threadsafe(task.add_done_callback, lambda _: finished.set())
        if not finished.wait(_CANCEL_GRACE_SECONDS):
            retire_loop_thread(self)
            # Stop this loop only once the abandoned task has actually unwound;
            # stopping it while the task is pending destroys the task and warns.
            self.loop.call_soon_threadsafe(
                task.add_done_callback,
                lambda _: self.loop.call_soon_threadsafe(self.loop.stop),
            )

    def retire(self) -> None:
        """Mark this loop abandoned (never used for new work again)."""
        self.retired = True

    def stop_when_idle(self) -> None:
        """Cancel pending work, then stop the loop (bounded graceful shutdown)."""

        def _stop() -> None:
            pending = [task for task in asyncio.all_tasks(self.loop) if not task.done()]
            for task in pending:
                task.cancel()
            if pending:
                self.loop.call_later(0.2, self.loop.stop)
            else:
                self.loop.stop()

        with contextlib.suppress(Exception):  # pragma: no cover - a wedged loop cannot receive this
            self.loop.call_soon_threadsafe(_stop)


_current_loop: _LoopThread | None = None
_current_lock = threading.Lock()


def get_loop_thread() -> _LoopThread:
    """Return the current persistent loop thread, creating one if needed."""
    global _current_loop
    with _current_lock:
        if _current_loop is None or _current_loop.retired:
            _current_loop = _LoopThread()
        return _current_loop


def retire_loop_thread(loop: _LoopThread) -> None:
    """Retire *loop* when it is still the current one (never a newer loop)."""
    global _current_loop
    with _current_lock:
        if _current_loop is loop:
            _current_loop = None
        loop.retire()


def reset_bridge() -> None:
    """Retire the current loop (test isolation / shutdown)."""
    global _current_loop
    with _current_lock:
        loop, _current_loop = _current_loop, None
    if loop is not None:
        loop.stop_when_idle()


def run_coroutine_on_loop(coro: Coroutine[Any, Any, Any], timeout: float) -> Any:
    """Run *coro* on the persistent loop, bounded and non-wedging."""
    loop = get_loop_thread()
    try:
        return loop.run(coro, timeout)
    except TimeoutError:
        _log.error(
            "judge.bridge_timeout",
            extra={"timeout_seconds": timeout},
        )
        raise


__all__ = [
    "DEFAULT_BRIDGE_TIMEOUT_SECONDS",
    "get_loop_thread",
    "reset_bridge",
    "retire_loop_thread",
    "run_coroutine_on_loop",
]
