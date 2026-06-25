"""@cancellable_node — LangGraph node wrapper for cancellation, timeout, and run_context guard.

Every node in a Modulo pipeline must be wrapped with this decorator. It enforces
four invariants:

1. Cancellation check: if state["run_context"]["cancelled"] is True before the node
   runs, raise RunCancelledError immediately without invoking the node function.
   Additionally, if the executor has registered a DB-backed cancellation check hook
   (via set_cancellation_check), it is called to verify against
   run.cancellation_requested — the authoritative source of truth.

2. Per-node timeout: the node coroutine is wrapped in asyncio.wait_for(coro, timeout).
   TimeoutError propagates to the run state machine.

3. Context-setter guard: if the returned state update includes a "run_context" key,
   and the node's role is not "context_setter", raise ContextSetterViolationError.
   This prevents agents from overwriting each other's run context.

4. Run-context write log: every context-setter write to run_context is recorded in
   ``state["_run_context_write_log"]`` as an ordered log entry with node name,
   timestamp, written fields, and last-write-wins semantics.  Non-context-setter
   violations are also logged as warnings.
"""

import asyncio
import functools
import logging
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

_log = logging.getLogger(__name__)


class RunCancelledError(RuntimeError):
    """Raised when a run has been cancelled before a node executes."""


class ContextSetterViolationError(RuntimeError):
    """Raised when a non-context-setter node attempts to write to run_context."""


# Async-safe hook for DB-backed cancellation check. Set per-run by PipelineExecutor.
# Using ContextVar ensures concurrent runs don't interfere — each asyncio task gets
# its own copy.
_cancellation_check_cv: ContextVar[Callable[[], Awaitable[bool]] | None] = ContextVar(
    "_cancellation_check", default=None
)


# Canonical write-log key in LangGraph state.
_RUN_CONTEXT_WRITE_LOG_KEY = "_run_context_write_log"


def set_cancellation_check(
    fn: Callable[[], Awaitable[bool]] | None,
) -> None:
    """Set the DB-backed cancellation check for the current asyncio task.

    Called by PipelineExecutor before graph execution; cleared in a finally block.
    Pass None to clear the hook.
    """
    _cancellation_check_cv.set(fn)


def _get_cancellation_check() -> Callable[[], Awaitable[bool]] | None:
    return _cancellation_check_cv.get()


def cancellable_node(
    *,
    timeout: float | None = None,
    role: str | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorate a LangGraph node function with cancellation, timeout, and context guard.

    Args:
        timeout: Maximum seconds the node may run. None means no timeout.
        role:    Node role string. Pass "context_setter" to allow run_context writes.
    """

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        async def wrapper(state: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
            # 1a. State-based cancellation check (fast path — no DB roundtrip)
            run_ctx: dict[str, Any] = state.get("run_context") or {}
            if run_ctx.get("cancelled", False):
                raise RunCancelledError(f"Run cancelled before node {fn.__name__!r} could execute.")

            # 1b. DB-backed cancellation check (authoritative source)
            db_check = _get_cancellation_check()
            if db_check is not None and await db_check():
                raise RunCancelledError(
                    f"Run cancelled (DB check) before node {fn.__name__!r} could execute."
                )

            # 2. Timeout-wrapped execution
            coro = fn(state, **kwargs)
            if timeout is not None:
                try:
                    result: dict[str, Any] = await asyncio.wait_for(coro, timeout=timeout)
                except TimeoutError:
                    raise TimeoutError(
                        f"Node {fn.__name__!r} exceeded {timeout}s timeout."
                    ) from None
            else:
                result = await coro

            # 3. Context-setter guard and write log
            if result and "run_context" in result:
                if role == "context_setter":
                    # Record write in the ordered write-log with last-write-wins semantics.
                    write_log: list[dict[str, Any]] = list(
                        state.get(_RUN_CONTEXT_WRITE_LOG_KEY) or []
                    )
                    written_fields = list(result["run_context"].keys())
                    write_log.append({
                        "node_name": fn.__name__,
                        "role": role,
                        "timestamp": datetime.now(UTC).isoformat(),
                        "written_fields": written_fields,
                    })
                    result[_RUN_CONTEXT_WRITE_LOG_KEY] = write_log

                    _log.info(
                        "run_context.write",
                        extra={
                            "node_name": fn.__name__,
                            "fields": written_fields,
                        },
                    )
                else:
                    # Non-context-setter violation — log warning and raise.
                    _log.warning(
                        "run_context.violation",
                        extra={
                            "node_name": fn.__name__,
                            "role": role,
                            "attempted_fields": list(result["run_context"].keys()),
                        },
                    )
                    raise ContextSetterViolationError(
                        f"Node {fn.__name__!r} (role={role!r}) returned a 'run_context' update. "
                        "Only nodes with role='context_setter' may modify run_context."
                    )

            return result

        return wrapper

    return decorator
