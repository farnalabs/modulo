"""Audit coverage for mutating MCP tools (FAR-1472, sweep 7 of 7).

The REST surface records its audit events through the ``audited(...)`` route
dependency in ``modulo.core.audit_coverage``. MCP tools never pass through
FastAPI: they call their ``_*_impl`` / CRUD helpers directly, so the route-layer
dependency never fires for them and most mutating tools wrote nothing at all.
``mcp_audited`` is the MCP counterpart — one coarse audit event per tool call::

    @mcp.tool(description="Delete a pipeline by ID.")
    @mcp_audited("pipeline_deleted", "pipeline", fail_closed=True)
    @_RETRY_DB
    async def delete_pipeline(pipeline_id: str) -> dict[str, Any]: ...

Placement matters: the decorator goes directly under ``@mcp.tool(...)`` and
OUTSIDE ``@_RETRY_DB`` / ``@_tool_db_shell(...)`` so the event is written
exactly once per call, after the tool's own retries and exception shell have
finished. Inside them it would fire once per retry attempt.

Fresh session, never the tool's session
---------------------------------------
The event is appended on its OWN session from the process-shared engine —
``core.audit_coverage.audit_session``, the same factory the REST dependency
uses — opened after the tool's mutation has already committed on its own
``_session`` transaction. Consequences are the same as on the REST side: the
append can neither roll back nor be blocked by the business transaction, and
it does not care that the tool left its session closed.

Actor: the authenticated MCP caller, never a fabricated one
-----------------------------------------------------------
Organisation and actor resolve from the request-scoped ContextVars that
``McpAuthMiddleware`` populates (``_ctx_org_id_val`` / ``_ctx_user_id_val`` /
``_ctx_role_val``). With no organisation there is nothing truthful to record,
so the event is skipped with a loud log; with no user id the event is written
with a NULL actor — exactly what the pre-existing MCP audit helpers
(``_emit_mcp_api_key_audit``, ``_append_mcp_threshold_denial_audit``) do.

Payload
-------
Deliberately coarse and identical for every annotated tool::

    {"tool": "delete_pipeline", "outcome": "success" | "error"}


Failure policy — mirrors ``modulo.core.audit_coverage``
-------------------------------------------------------
* **Establishing the audit transaction** (fresh session + RLS context) is
  always logged-and-continued for both policies. It runs *after* the tool's
  own transaction committed on the same engine, so a failure to open it must
  never replace the result the caller is about to see.
* **The append itself (and its commit)** honours ``fail_closed``. With
  ``fail_closed=True`` the error propagates, so a destructive mutation the
  audit chain refused is reported to the caller instead of succeeding
  silently; with ``fail_closed=False`` it is logged and the call carries on
  (fail-open, with a log).
* **The tool's own failure always wins**: when the tool raised, the attempt is
  recorded best-effort (outcome ``error``, never fail-closed) and the original
  exception keeps propagating — an audit failure must never replace the error
  the caller is about to see.
* ``asyncio.CancelledError`` always propagates; a cancelled call is never
  turned into an audit write.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any, TypeVar, cast

from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.audit_logger import append_audit_event
from modulo.db.rls import set_rls_org, set_rls_user_context

_log = logging.getLogger(__name__)

__all__ = ["mcp_audited"]

#: How the tool call finished, as recorded in the coarse payload:
#: ``"success"`` when it returned a non-error result, ``"error"`` when it
#: returned an error envelope (``{"error": ...}``) or raised.
AuditOutcome = str

#: A decorated async MCP tool.
ToolFn = TypeVar("ToolFn", bound=Callable[..., Awaitable[Any]])

#: Prefix of the log keys this module emits, so append failures are greppable.
_AUDIT_LOG_PREFIX = "mcp_audit"


def _log_key(event_type: str) -> str:
    """Stable log key for an event type's append failures."""
    return f"{_AUDIT_LOG_PREFIX}.{event_type}.append_failed"


@asynccontextmanager
async def _fresh_session() -> AsyncGenerator[AsyncSession, None]:
    """Yield the fresh session one audit event is written on.

    Delegates to ``core.audit_coverage.audit_session`` — the REST dependency's
    own factory on the process-shared engine — so both surfaces append through
    one session policy. Imported lazily, mirroring why ``audit_session`` itself
    keeps its engine import lazy: resolving it at *our* import time would pull
    the process engine in as a side effect of importing the MCP server.
    """
    from modulo.core.audit_coverage import audit_session

    async with asynccontextmanager(audit_session)() as session:
        yield session


async def _record(
    *,
    event_type: str,
    resource_type: str,
    tool_name: str,
    outcome: AuditOutcome,
    fail_closed: bool,
) -> None:
    """Append one coarse audit event for a completed MCP tool call.

    Failure-isolated by construction: every segment either applies the
    caller's policy or logs and continues, so this never raises except on
    ``CancelledError`` (and on a genuine append refusal when the caller asked
    for ``fail_closed`` on a successful call).
    """
    # Lazy: mcp_server imports THIS module at its import time, so the
    # request-scoped ContextVars can only be read once it is fully loaded.
    from modulo.api import mcp_server as _mcp

    log_key = _log_key(event_type)
    extra = {
        "event_type": event_type,
        "resource_type": resource_type,
        "tool": tool_name,
    }

    try:
        org_id = _mcp._ctx_org_id_val()
    except _mcp.McpAuthContextError:
        # No authenticated tenant: there is no truthful row to write.
        _log.warning(log_key, extra={**extra, "reason": "no_org_context"})
        return

    try:
        actor_user_id = _mcp._ctx_user_id_val()
    except _mcp.McpAuthContextError:
        # Never fabricate an actor — record the event with a null one.
        actor_user_id = None
    org_role = _mcp._ctx_role_val()
    payload: dict[str, Any] = {"tool": tool_name, "outcome": outcome}
    #: Only a SUCCESSFUL call can be failed by its own missing audit record;
    #: when the tool already failed its own error must survive unchanged.
    apply_fail_closed = fail_closed and outcome == "success"
    extra_org = {**extra, "org_id": str(org_id)}

    stack = AsyncExitStack()
    try:
        # Segment 1: establish the audit transaction (fresh session, BEGIN,
        # RLS context). Always logged-and-continued for both policies.
        try:
            session = await stack.enter_async_context(_fresh_session())
            await stack.enter_async_context(session.begin())
            await set_rls_org(session, org_id)
            if actor_user_id is not None:
                await set_rls_user_context(session, actor_user_id, org_role or "")
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.warning(log_key, extra={**extra_org, "stage": "session_setup"}, exc_info=True)
            return

        # Segment 2: append the event to the chain (the commit happens when
        # the stack unwinds below). Here the caller's failure policy applies.
        try:
            await append_audit_event(
                session,
                org_id=org_id,
                event_type=event_type,
                actor_user_id=actor_user_id,
                resource_type=resource_type,
                payload_json=payload,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            if apply_fail_closed:
                raise
            _log.warning(log_key, extra=extra_org, exc_info=True)
    finally:
        # COMMIT on the success path; rollback/close otherwise. A close failure
        # must never mask the append result (or replace a fail-closed raise).
        try:
            await stack.aclose()
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.warning(f"{log_key}.close_failed", extra=extra, exc_info=True)


def mcp_audited(
    event_type: str,
    resource_type: str,
    *,
    fail_closed: bool = False,
) -> Callable[[ToolFn], ToolFn]:
    """Decorator factory: record one audit event for an MCP tool call.

    Use it on a mutating tool, directly under ``@mcp.tool(...)``::

        @mcp.tool(description="Delete a pipeline by ID.")
        @mcp_audited("pipeline_deleted", "pipeline", fail_closed=True)
        @_RETRY_DB
        async def delete_pipeline(pipeline_id: str) -> dict[str, Any]: ...

    Args:
        event_type: Chained-audit event type (``audit_events.event_type``).
            Follow the REST surface's domain naming (``pipeline_deleted``,
            ``parameter_schema_created``, ...) so both surfaces are readable
            in one audit trail.
        resource_type: Coarse resource kind the event is about.
        fail_closed: Re-raise an append failure instead of logging it. Use it
            for destructive and credential-bearing tools, where a mutation the
            audit chain refused must not look like a success.

    Returns:
        A decorator preserving the tool's name and signature, so FastMCP still
        derives the correct tool schema from ``@mcp.tool(...)``.
    """
    if not event_type.strip():
        raise ValueError("mcp_audited() requires a non-empty event_type")
    if not resource_type.strip():
        raise ValueError("mcp_audited() requires a non-empty resource_type")

    def decorator(fn: ToolFn) -> ToolFn:
        tool_name = fn.__name__

        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            """Record ``event_type`` once the tool body has finished."""
            try:
                result = await fn(*args, **kwargs)
            except (GeneratorExit, asyncio.CancelledError):
                # Teardown without a completed call: no business outcome to
                # record, and no await may run during cancellation.
                raise
            except BaseException:
                # The tool failed outright. Record the attempt best-effort —
                # an audit failure must never replace its error.
                await _record(
                    event_type=event_type,
                    resource_type=resource_type,
                    tool_name=tool_name,
                    outcome="error",
                    fail_closed=False,
                )
                raise
            outcome: AuditOutcome = "error" if isinstance(result, Mapping) and "error" in result else "success"
            await _record(
                event_type=event_type,
                resource_type=resource_type,
                tool_name=tool_name,
                outcome=outcome,
                fail_closed=fail_closed,
            )
            return result

        return cast(ToolFn, wrapper)

    return decorator
