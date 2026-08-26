"""@cancellable_node — LangGraph node wrapper for cancellation, timeout, and run_context guard.

Every node in a Modulo pipeline must be wrapped with this decorator. It enforces
six invariants:

1. Cancellation check: if state["run_context"]["cancelled"] is True before the node
   runs, raise RunCancelledError immediately without invoking the node function.
   Additionally, if the executor has registered a DB-backed cancellation check hook
   (via set_cancellation_check), it is called to verify against
   run.cancellation_requested — the authoritative source of truth. DB check failures
   are caught and logged as warnings; the run continues (conservative degrade).

2. Per-node timeout: the node coroutine is wrapped in asyncio.wait_for(coro, timeout).
   TimeoutError propagates to the run state machine.

3. Context-setter guard: if the returned state update includes a "run_context" key,
   and the node's role is not "context_setter", raise ContextSetterViolationError.
   This prevents agents from overwriting each other's run context.

4. Reserved-key protection: context-setter agents may not write to internal reserved
   keys (cancelled, input, _pipeline_default_autonomy, _run_context_write_log).
   Attempts are silently stripped and logged as warnings.

5. Run-context write log: every context-setter write to run_context is recorded in
   ``state["_run_context_write_log"]`` as an ordered log entry with node name,
   timestamp, written fields, and last-write-wins semantics.  Non-context-setter
   violations are also logged as warnings.

6. Graceful DB degradation: if the DB-backed cancellation check hook raises an
   exception (e.g. connection failure), the error is caught, logged with
   exc_info=True, and the run continues as if not cancelled.
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

# Async-safe hook for audit-event dispatch. Set per-run by PipelineExecutor. When a
# non-context-setter node attempts to write run_context, the hook is invoked with
# {"node_id", "role", "attempted_keys"} so the executor can record a
# `context_write_by_non_setter` audit event (§8.18). Failures never propagate — the
# hook must not mask the violation error raised by the decorator.
_AuditHook = Callable[[dict[str, Any]], Awaitable[None]]
_audit_hook_cv: ContextVar[_AuditHook | None] = ContextVar("_audit_hook", default=None)

# ModelBackendHub for the current run — provides model backends to make_node_fn.
_model_backend_hub_cv: ContextVar[Any | None] = ContextVar("_model_backend_hub", default=None)

# ConnectorHub for the current run — provides connector instances to connector nodes.
_connector_hub_cv: ContextVar[Any | None] = ContextVar("_connector_hub", default=None)


def get_connector_hub() -> Any | None:
    """Return the ConnectorHub for the current run, or None if not initialised."""
    return _connector_hub_cv.get()


def set_connector_hub(hub: Any | None) -> None:
    """Set the ConnectorHub for the current run."""
    _connector_hub_cv.set(hub)


# Canonical write-log key in LangGraph state.
_RUN_CONTEXT_WRITE_LOG_KEY = "_run_context_write_log"

# Keys in run_context that context-setter agents may NOT modify.
_RESERVED_RUN_CONTEXT_KEYS = frozenset(
    {
        "cancelled",
        "input",
        "_pipeline_default_autonomy",
        "_run_context_write_log",
    }
)


def get_model_backend_hub() -> Any | None:
    return _model_backend_hub_cv.get()


def set_model_backend_hub(hub: Any | None) -> None:
    _model_backend_hub_cv.set(hub)


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


def set_audit_hook(fn: _AuditHook | None) -> None:
    """Set the audit-event dispatch hook for the current asyncio task.

    Called by PipelineExecutor before graph execution; cleared in a finally block.
    The hook receives ``{"node_id", "role", "attempted_keys"}`` when a
    non-context-setter node attempts to write run_context. Pass None to clear.
    """
    _audit_hook_cv.set(fn)


def _get_audit_hook() -> _AuditHook | None:
    return _audit_hook_cv.get()


async def _dispatch_audit_warning(
    node_name: str,
    role: str | None,
    attempted: list[str],
) -> None:
    """Invoke the audit hook for a context-write violation, swallowing failures.

    The hook is best-effort: a slow or failing audit write must never mask the
    ContextSetterViolationError raised by the decorator, so exceptions are logged
    and the violation proceeds. asyncio.CancelledError propagates.
    """
    hook = _get_audit_hook()
    if hook is None:
        return
    try:
        await hook(
            {
                "node_id": node_name,
                "role": role,
                "attempted_keys": attempted,
            }
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.warning(
            "run_context.audit_hook_failed",
            extra={
                "node_name": node_name,
                "attempted_fields": attempted,
            },
            exc_info=True,
        )


async def _raise_if_state_cancelled(fn_name: str, run_ctx: dict[str, Any]) -> None:
    """1a. State-based cancellation check (fast path — no DB roundtrip)."""
    if run_ctx.get("cancelled", False):
        raise RunCancelledError(f"Run cancelled before node {fn_name!r} could execute.")


async def _raise_if_db_cancelled(fn_name: str) -> None:
    """1b. DB-backed cancellation check (authoritative source).

    Hook failures are caught, logged as warnings, and the run continues
    (conservative degrade). asyncio.CancelledError propagates.
    """
    db_check = _get_cancellation_check()
    if db_check is None:
        return
    try:
        db_cancelled = await db_check()
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.warning(
            "run_context.cancellation_check_failed",
            extra={"node_name": fn_name},
            exc_info=True,
        )
        return
    if db_cancelled:
        raise RunCancelledError(f"Run cancelled (DB check) before node {fn_name!r} could execute.")


async def _execute_node(
    fn: Callable[..., Any],
    state: dict[str, Any],
    kwargs: dict[str, Any],
    timeout_seconds: float | None,
    fn_name: str,
) -> dict[str, Any]:
    """2. Timeout-wrapped execution of the wrapped node coroutine."""
    coro = fn(state, **kwargs)
    if timeout_seconds is not None:
        try:
            result: dict[str, Any] = await asyncio.wait_for(coro, timeout=timeout_seconds)
        except TimeoutError:
            raise TimeoutError(f"Node {fn_name!r} exceeded {timeout_seconds}s timeout.") from None
    else:
        result = await coro
    return result


def _strip_reserved_keys(fn_name: str, result_rc: dict[str, Any]) -> list[str]:
    """Strip reserved keys a context-setter may not modify; return those stripped."""
    attempted_reserved = [k for k in result_rc if k in _RESERVED_RUN_CONTEXT_KEYS]
    for k in attempted_reserved:
        result_rc.pop(k)
    if attempted_reserved:
        _log.warning(
            "run_context.reserved_key_attempt",
            extra={
                "node_name": fn_name,
                "reserved_keys": attempted_reserved,
            },
        )
    return attempted_reserved


def _record_write_log(
    state: dict[str, Any],
    result: dict[str, Any],
    fn_name: str,
    role: str | None,
    result_rc: dict[str, Any],
) -> None:
    """Append an ordered write-log entry for a context-setter's run_context write."""
    write_log: list[dict[str, Any]] = list(state.get(_RUN_CONTEXT_WRITE_LOG_KEY) or [])
    written_fields = list(result_rc.keys())
    write_log.append(
        {
            "node_name": fn_name,
            "role": role,
            "timestamp": datetime.now(UTC).isoformat(),
            "written_fields": written_fields,
        }
    )
    result[_RUN_CONTEXT_WRITE_LOG_KEY] = write_log
    _log.info(
        "run_context.write",
        extra={
            "node_name": fn_name,
            "fields": written_fields,
        },
    )


async def _handle_context_setter(
    result: dict[str, Any],
    state: dict[str, Any],
    fn_name: str,
    role: str | None,
) -> None:
    """3. Apply context-setter guard: strip reserved keys and record the write log."""
    result_rc: dict[str, Any] = result["run_context"]
    _strip_reserved_keys(fn_name, result_rc)
    if result_rc:
        _record_write_log(state, result, fn_name, role, result_rc)
    result["run_context"] = result_rc


async def _raise_context_violation(
    result: dict[str, Any],
    fn_name: str,
    role: str | None,
) -> None:
    """3. Non-context-setter violation — dispatch audit event, log, and raise."""
    attempted = list(result["run_context"].keys())
    await _dispatch_audit_warning(fn_name, role, attempted)
    _log.warning(
        "run_context.violation",
        extra={
            "node_name": fn_name,
            "role": role,
            "attempted_fields": attempted,
        },
    )
    raise ContextSetterViolationError(
        f"Node {fn_name!r} (role={role!r}) returned a 'run_context' update. "
        "Only nodes with role='context_setter' may modify run_context."
    )


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
            run_ctx: dict[str, Any] = state.get("run_context") or {}
            await _raise_if_state_cancelled(fn.__name__, run_ctx)
            await _raise_if_db_cancelled(fn.__name__)

            result = await _execute_node(fn, state, kwargs, timeout, fn.__name__)

            # 3. Context-setter guard and write log
            if result and "run_context" in result and result["run_context"] is not None:
                if role == "context_setter":
                    await _handle_context_setter(result, state, fn.__name__, role)
                else:
                    await _raise_context_violation(result, fn.__name__, role)

            return result

        return wrapper

    return decorator
