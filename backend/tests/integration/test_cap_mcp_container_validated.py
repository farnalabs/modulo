"""FAR-1722 cap-mcp: the MCP server surface, container-validated.

The MCP capability (product map ``feat-mcp``) previously had only mock/BDD
evidence: the unit suites patch ``mcp_server._session`` / ``validate_current_auth``
and the BDD scenarios drive the real handler functions with the auth
re-validation and DB seams patched. Neither observes the real wire surface.

This module closes that gap against REAL Postgres (testcontainers):

* the production ASGI app (``main.app`` with ``/mcp`` mounted by
  ``build_mcp_asgi_app()``) is driven through the real MCP streamable-HTTP
  transport by the official ``mcp`` client — ``initialize``, ``tools/list``
  and ``tools/call`` — so FastMCP's dispatch layer, ``McpAuthMiddleware``,
  ``RateLimitMiddleware`` and the ``mcp_server`` plan gate all execute for
  real;
* the credential is a REAL ``mk_`` org API-key row, so the SECURITY DEFINER
  ``lookup_api_key_org`` prefix lookup, the RLS-scoped re-validation, the
  live-role clamp (ADR 047) and ``validate_current_auth``'s per-call
  re-validation all run against the container;
* ``list_pipelines`` reads committed rows through ``mcp_server._session``
  (RLS org context on a real pooled session): the response must contain the
  seeded pipeline and must never contain a pipeline owned by another
  organisation;
* the unauthenticated and revoked/wrong-secret arms of the same middleware
  are asserted over the real HTTP surface (401 + the RFC 9728
  ``WWW-Authenticate`` challenge).

The FastMCP session manager is wired exactly the way ``main.py``'s lifespan
wires it — a long-lived anyio task group assigned to
``session_manager._task_group`` — so no part of the transport is stubbed.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Any

import anyio
import httpx
import pytest
import pytest_asyncio
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from modulo.auth.api_key import generate_api_key

pytestmark = pytest.mark.integration

#: The production mount point (``main.app.mount("/mcp", ...)``). The host must
#: be a loopback name carrying a port: FastMCP's default DNS-rebinding guard
#: only allows ``localhost:*`` / ``127.0.0.1:*`` / ``[::1]:*`` Host headers,
#: and httpx omits the port for a default-port URL.
_MCP_URL = "http://localhost:8000/mcp/"
_MCP_HEALTHZ_URL = "http://localhost:8000/mcp/healthz"
_MCP_INIT_BODY = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "far-1722-cap-mcp", "version": "0"},
    },
}
_MCP_ACCEPT = "application/json, text/event-stream"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def mcp_asgi_app() -> Any:
    """The real FastAPI app, whose ``/mcp`` mount is ``build_mcp_asgi_app()``."""
    from modulo.api.main import app

    return app


@pytest_asyncio.fixture(scope="module")
async def mcp_session_manager_task_group(mcp_asgi_app: Any) -> AsyncIterator[None]:
    """Start the MCP session manager the same way production's lifespan does.

    ``build_mcp_asgi_app()`` deliberately ships NO lifespan (the parent
    ``main.app`` owns it), and ``StreamableHTTPSessionManager._handle_request``
    refuses every request while ``_task_group`` is unset. ``main.py``'s
    lifespan therefore creates an anyio task group and assigns it to the
    private integration slot.

    The task group must be ENTERED and EXITED inside one task: anyio's cancel
    scope refuses to unwind in a different task, and pytest-asyncio runs a
    module-scoped fixture's setup and teardown in different tasks. So the
    group lives on a dedicated background task that parks in
    ``sleep_forever()`` until teardown cancels it. ``TaskGroup._spawn`` copies
    the CALLER's context (``loop.create_task`` with no explicit context), so a
    request's auth contextvars still reach the per-request server task.
    """
    from modulo.api.mcp_server import mcp

    session_manager = mcp.session_manager
    started = asyncio.Event()

    async def _park_task_group() -> None:
        async with anyio.create_task_group() as task_group:
            session_manager._task_group = task_group
            started.set()
            await anyio.sleep_forever()

    runner = asyncio.create_task(_park_task_group())
    # Fail loudly rather than hang if the parked group never publishes itself.
    await asyncio.wait_for(started.wait(), timeout=10)
    try:
        yield
    finally:
        runner.cancel()
        with suppress(asyncio.CancelledError):
            await runner
        session_manager._task_group = None


async def _mint_org_api_key(
    db_engine: AsyncEngine,
    org_id: uuid.UUID,
    account_id: uuid.UUID,
    *,
    name: str,
) -> tuple[str, uuid.UUID]:
    """Insert a committed, non-revoked ``org``-scope API-key row; return (full_key, key_id)."""
    full_key, prefix, hashed = generate_api_key()
    key_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO org_api_keys (id, organisation_id, name, lookup_prefix, "
                "hashed_secret, role, account_id, scope, expires_at) "
                "VALUES (:id, :oid, :name, :prefix, :hashed, 'operator', :uid, 'org', "
                "(CURRENT_TIMESTAMP + INTERVAL '30 days'))"
            ),
            {
                "id": str(key_id),
                "oid": str(org_id),
                "name": name,
                "prefix": prefix,
                "hashed": hashed,
                "uid": str(account_id),
            },
        )
    return full_key, key_id


@pytest_asyncio.fixture(scope="module")
async def mcp_api_key(
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> AsyncIterator[str]:
    """A real, valid ``mk_`` org API key for ``test_org`` (operator role)."""
    full_key, key_id = await _mint_org_api_key(
        db_engine,
        test_org,
        test_user,
        name=f"cap-mcp-container-{uuid.uuid4().hex[:8]}",
    )
    try:
        yield full_key
    finally:
        async with db_engine.connect() as conn, conn.begin():
            await conn.execute(
                text("DELETE FROM org_api_keys WHERE id = :kid"),
                {"kid": str(key_id)},
            )


@pytest_asyncio.fixture
async def other_org_pipeline(
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> AsyncIterator[uuid.UUID]:
    """A committed pipeline owned by a DIFFERENT organisation (tenancy probe)."""
    other_org_id = uuid.uuid4()
    other_pipeline_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text("INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :name, :slug, '{}'::json)"),
            {
                "id": str(other_org_id),
                "name": "Cap MCP Other Org",
                "slug": f"cap-mcp-other-{other_org_id.hex[:8]}",
            },
        )
        await conn.execute(
            text(
                "INSERT INTO pipelines (id, organisation_id, name, account_id, "
                "max_concurrent_runs, lock_wait_timeout_seconds, node_timeout_seconds, "
                "run_context_defaults, graph_nodes_json) "
                "VALUES (:id, :oid, :name, :uid, 10, 30, 300, '{}'::json, '[]'::json)"
            ),
            {
                "id": str(other_pipeline_id),
                "oid": str(other_org_id),
                "name": f"cap-mcp-other-org-pipeline-{other_pipeline_id.hex[:8]}",
                "uid": str(test_user),
            },
        )
    try:
        yield other_pipeline_id
    finally:
        async with db_engine.connect() as conn, conn.begin():
            await conn.execute(
                text("DELETE FROM pipeline_snapshots WHERE pipeline_id = :pid"),
                {"pid": str(other_pipeline_id)},
            )
            await conn.execute(
                text("DELETE FROM pipelines WHERE id = :pid"),
                {"pid": str(other_pipeline_id)},
            )
            await conn.execute(
                text("DELETE FROM organisations WHERE id = :oid AND id <> :keep"),
                {"oid": str(other_org_id), "keep": str(test_org)},
            )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _asgi_client(app: Any, headers: dict[str, str] | None = None) -> httpx.AsyncClient:
    """httpx client whose transport is the ASGI app itself (no network socket)."""
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        headers=headers,
        timeout=30.0,
    )


@asynccontextmanager
async def _mcp_client_session(app: Any, token: str) -> AsyncIterator[tuple[ClientSession, Any]]:
    """A real MCP client session over the real streamable-HTTP surface.

    Yields ``(session, initialize_result)`` — the handshake result is the
    client's own return value from ``ClientSession.initialize()``.
    """
    http_client = _asgi_client(app, headers={"Authorization": f"Bearer {token}"})
    async with http_client, streamable_http_client(_MCP_URL, http_client=http_client) as streams:
        read_stream, write_stream, _get_session_id = streams
        async with ClientSession(read_stream, write_stream) as session:
            initialize_result = await session.initialize()
            yield session, initialize_result


def _tool_payload_json(result: Any) -> Any:
    """Decode the single JSON-text content block a Modulo MCP tool returns."""
    assert result.isError is False, result
    assert len(result.content) == 1, result
    return json.loads(result.content[0].text)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_initialize_and_tools_list_over_real_transport(
    mcp_asgi_app: Any,
    mcp_session_manager_task_group: None,
    mcp_api_key: str,
) -> None:
    """The real FastMCP server completes the MCP handshake and lists real tools."""
    async with _mcp_client_session(mcp_asgi_app, mcp_api_key) as (session, initialize_result):
        assert initialize_result.serverInfo.name == "Modulo"

        tools = await session.list_tools()
        names = {tool.name for tool in tools.tools}
        # Read-surface tools the capability map names, resolved through the
        # real FastMCP registry rather than a mocked tool table.
        assert {"list_pipelines", "get_pipeline_graph", "search_library"} <= names


async def test_list_pipelines_call_returns_real_db_rows(
    mcp_asgi_app: Any,
    mcp_session_manager_task_group: None,
    mcp_api_key: str,
    test_pipeline: uuid.UUID,
) -> None:
    """A ``tools/call`` round-trip reads committed rows out of real Postgres.

    This is the container-validated claim: the tool executes behind the real
    auth middleware with a real ``mk_`` credential, opens a real RLS-scoped
    session, and returns the seeded pipeline by id and name.
    """
    async with _mcp_client_session(mcp_asgi_app, mcp_api_key) as (session, _initialize_result):
        result = await session.call_tool("list_pipelines", {"limit": 100})
        payload = _tool_payload_json(result)

    assert "error" not in payload, payload
    returned_ids = {row["id"] for row in payload["data"]}
    returned_names = {row["name"] for row in payload["data"]}
    assert str(test_pipeline) in returned_ids
    assert "Integration Test Pipeline" in returned_names


async def test_tool_call_is_org_scoped_against_real_rls(
    mcp_asgi_app: Any,
    mcp_session_manager_task_group: None,
    mcp_api_key: str,
    other_org_pipeline: uuid.UUID,
) -> None:
    """A pipeline owned by another organisation never appears in the tool result.

    ``list_pipelines`` filters on the contextvar org AND runs under the RLS
    org context ``mcp_server._session`` sets — both legs are real here, so a
    regression in either (a dropped org filter, a lost RLS context) makes this
    fail.
    """
    async with _mcp_client_session(mcp_asgi_app, mcp_api_key) as (session, _initialize_result):
        result = await session.call_tool("list_pipelines", {"limit": 100})
        payload = _tool_payload_json(result)

    assert "error" not in payload, payload
    returned_ids = {row["id"] for row in payload["data"]}
    assert str(other_org_pipeline) not in returned_ids


async def test_missing_bearer_token_returns_401_with_resource_metadata_challenge(
    mcp_asgi_app: Any,
    mcp_session_manager_task_group: None,
) -> None:
    """The unauthenticated arm of the real middleware: 401 + RFC 9728 challenge."""
    async with _asgi_client(mcp_asgi_app) as client:
        response = await client.post(_MCP_URL, json=_MCP_INIT_BODY, headers={"Accept": _MCP_ACCEPT})

    assert response.status_code == 401
    assert response.json()["error"] == "unauthorized"
    assert "Bearer" in response.headers["WWW-Authenticate"]
    assert "resource_metadata" in response.headers["WWW-Authenticate"]


async def test_revoked_api_key_returns_401_over_real_transport(
    mcp_asgi_app: Any,
    mcp_session_manager_task_group: None,
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    """A key revoked in the database dies at the middleware on the very next call."""
    full_key, key_id = await _mint_org_api_key(
        db_engine,
        test_org,
        test_user,
        name=f"cap-mcp-revoked-{uuid.uuid4().hex[:8]}",
    )
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text("UPDATE org_api_keys SET revoked_at = CURRENT_TIMESTAMP WHERE id = :kid"),
            {"kid": str(key_id)},
        )
    try:
        async with _asgi_client(mcp_asgi_app, headers={"Authorization": f"Bearer {full_key}"}) as client:
            response = await client.post(_MCP_URL, json=_MCP_INIT_BODY, headers={"Accept": _MCP_ACCEPT})

        assert response.status_code == 401
        assert response.json()["error"] == "unauthorized"
        assert "revoked" in response.json()["detail"].lower()
    finally:
        async with db_engine.connect() as conn, conn.begin():
            await conn.execute(
                text("DELETE FROM org_api_keys WHERE id = :kid"),
                {"kid": str(key_id)},
            )


async def test_wrong_secret_returns_401_over_real_transport(
    mcp_asgi_app: Any,
    mcp_session_manager_task_group: None,
    mcp_api_key: str,
) -> None:
    """A token with a real lookup prefix but a wrong secret is rejected 401."""
    wrong_secret_token = f"{mcp_api_key[:11]}{'z' * 32}"
    async with _asgi_client(mcp_asgi_app, headers={"Authorization": f"Bearer {wrong_secret_token}"}) as client:
        response = await client.post(_MCP_URL, json=_MCP_INIT_BODY, headers={"Accept": _MCP_ACCEPT})

    assert response.status_code == 401
    assert response.json()["error"] == "unauthorized"


async def test_mcp_healthz_is_served_unauthenticated(
    mcp_asgi_app: Any,
    mcp_session_manager_task_group: None,
) -> None:
    """The orchestrator health probe bypasses the bearer requirement."""
    async with _asgi_client(mcp_asgi_app) as client:
        response = await client.get(_MCP_HEALTHZ_URL)

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
