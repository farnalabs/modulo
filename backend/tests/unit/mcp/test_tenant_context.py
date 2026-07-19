"""Fail-closed tenant-context resolution for the MCP server.

Security review 2026-07-09, Finding 4: tenant identity for MCP tool calls
must come exclusively from request-scoped ContextVars set by
McpAuthMiddleware. There is no process-global fallback and no placeholder
org — missing context must raise, never resolve to another tenant.
"""

import asyncio
import contextvars
import uuid

import pytest

from modulo.api import mcp_server
from modulo.api.mcp_server import (
    McpAuthContextError,
    _ctx_auth_token,
    _ctx_auth_type,
    _ctx_org_id,
    _ctx_org_id_val,
    _ctx_role_val,
    _ctx_user_id_val,
    validate_current_auth,
)
from modulo.core.mcp.scope_validator import MCPAuthorizationError, check_tool_scope


class TestFailClosedResolution:
    """Missing tenant context raises instead of returning a placeholder/last org."""

    def test_org_id_raises_when_context_never_set(self) -> None:
        ctx = contextvars.Context()
        with pytest.raises(McpAuthContextError):
            ctx.run(_ctx_org_id_val)

    def test_org_id_raises_when_context_reset_to_none(self) -> None:
        # Existing tests reset vars with .set(None) — a stored None must also fail closed.
        token = _ctx_org_id.set(None)  # type: ignore[arg-type]
        try:
            with pytest.raises(McpAuthContextError):
                _ctx_org_id_val()
        finally:
            _ctx_org_id.reset(token)

    def test_user_id_raises_when_context_never_set(self) -> None:
        ctx = contextvars.Context()
        with pytest.raises(McpAuthContextError):
            ctx.run(_ctx_user_id_val)

    def test_role_is_none_when_unset_and_scope_check_rejects_it(self) -> None:
        ctx = contextvars.Context()
        role = ctx.run(_ctx_role_val)
        assert role is None
        with pytest.raises(MCPAuthorizationError):
            check_tool_scope(role, "trigger_pipeline")

    def test_no_module_level_fallback_globals_exist(self) -> None:
        for name in (
            "_auth_token_fallback",
            "_auth_org_id_fallback",
            "_auth_user_id_fallback",
            "_auth_role_fallback",
            "_PLACEHOLDER_ORG_ID",
        ):
            assert not hasattr(mcp_server, name), f"process-global fallback {name} must not exist"

    async def test_validate_current_auth_fails_closed_without_org(self) -> None:
        # Even with a plausible token present, a missing org context means
        # the request is treated as unauthenticated — no DB lookup, no fallback.
        t_type = _ctx_auth_type.set("api_key")
        t_token = _ctx_auth_token.set("mk_testprefix_testsecretkey1234567890abc")
        t_org = _ctx_org_id.set(None)  # type: ignore[arg-type]
        try:
            assert await validate_current_auth() is False
        finally:
            _ctx_auth_type.reset(t_type)
            _ctx_auth_token.reset(t_token)
            _ctx_org_id.reset(t_org)


class TestContextVarIsolation:
    """Concurrent authenticated contexts resolve their own org — never each other's."""

    async def test_concurrent_tasks_resolve_own_org(self) -> None:
        org_a = uuid.uuid4()
        org_b = uuid.uuid4()
        results: dict[str, uuid.UUID] = {}
        barrier = asyncio.Barrier(2)

        async def request(name: str, org: uuid.UUID) -> None:
            # Simulates McpAuthMiddleware setting the org for this request.
            _ctx_org_id.set(org)
            # Force interleaving so both requests are mid-flight together.
            await barrier.wait()

            async def handler() -> uuid.UUID:
                # Simulates FastMCP spawning the tool handler in a child task:
                # asyncio copies the current context at task-creation time.
                await asyncio.sleep(0)
                return _ctx_org_id_val()

            results[name] = await asyncio.create_task(handler())

        await asyncio.gather(request("a", org_a), request("b", org_b))
        assert results == {"a": org_a, "b": org_b}

    async def test_child_task_spawned_before_auth_fails_closed(self) -> None:
        # A task created from a context without auth must not see an org set later
        # elsewhere — it fails closed instead of resolving to the wrong tenant.
        async def unauthenticated_handler() -> uuid.UUID:
            await asyncio.sleep(0)
            return _ctx_org_id_val()

        empty_ctx = contextvars.Context()
        task: asyncio.Task[uuid.UUID] = asyncio.get_running_loop().create_task(
            unauthenticated_handler(), context=empty_ctx
        )
        with pytest.raises(McpAuthContextError):
            await task
