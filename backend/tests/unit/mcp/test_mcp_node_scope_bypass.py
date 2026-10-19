"""FAR-1976: node ``allowed_tools`` narrowing for the chokepoint-bypassing tools.

The read-only MCP tools pinned in
``tests/unit/test_mcp_structural_coverage._TOOLS_NOT_ROUTED_THROUGH_CHOKEPOINT``
never reach ``check_tool_scope``, so they also used to skip its node-level
``allowed_tools`` narrowing leg. Each handler now calls
``_check_node_allowed_tools`` at its top; these tests drive the REAL handler
path (no mock of the gate) so the enforcement fails if the helper call is
removed:

* a node whose ``allowed_tools`` excludes the tool is DENIED;
* a node whose ``allowed_tools`` includes the tool is ALLOWED;
* no node scope (``None``) is unrestricted/legacy.
"""

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from modulo.api.mcp_server import (
    _ctx_node_allowed_tools,
    get_available_features,
    get_integration_status,
    get_pipeline_graph_tool,
    get_run_status,
    list_pipelines_tool,
    list_schemas,
    resource_pipeline_snapshot_detail,
    search_documentation,
    validate_payload,
)
from tests.unit.mcp.helpers import AuthContext, make_session_context

# A pipeline list page shaped like ``modulo.db.crud.base.PageResult``.
_PAGE = SimpleNamespace(items=[], total=0, next_cursor=None, has_more=False)
_PIPELINE_ID = "11111111-1111-1111-1111-111111111111"
_RUN_ID = "33333333-3333-3333-3333-333333333333"
_SNAPSHOT_ID = "22222222-2222-2222-2222-222222222222"

#: Every pinned chokepoint-bypassing tool: the tool name it enforces under
#: (the ``@mcp.tool(name=...)`` value, not the Python symbol), the REAL handler,
#: and the arguments needed to reach its scope check. The structural guard
#: pins this exact set in
#: ``tests/unit/test_mcp_structural_coverage._TOOLS_NOT_ROUTED_THROUGH_CHOKEPOINT``;
#: a new bypass tool added there without a case here leaves its denial arm
#: unexecuted, which the changed-lines coverage gate then fails.
_BYPASS_TOOL_CASES = [
    ("get_available_features", get_available_features, {}),
    ("get_integration_status", get_integration_status, {}),
    ("get_pipeline_graph", get_pipeline_graph_tool, {"pipeline_id": _PIPELINE_ID}),
    ("get_run_status", get_run_status, {"run_id": _RUN_ID}),
    ("list_pipelines", list_pipelines_tool, {}),
    ("list_schemas", list_schemas, {}),
    ("search_documentation", search_documentation, {"query": "pipelines"}),
    ("validate_payload", validate_payload, {"schema_id": _PIPELINE_ID, "payload": {}}),
]


@contextmanager
def _node_scope(tools: list[str] | None):
    """Set + restore the request-scoped node allowed_tools ContextVar."""
    token = _ctx_node_allowed_tools.set(tools)
    try:
        yield
    finally:
        _ctx_node_allowed_tools.reset(token)


class TestBypassToolNodeScopeEnforcement(AuthContext):
    """Real handler path: the node allowed_tools leg is enforced."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("allowed", [["create_pipeline"], ["get_pipeline_graph"], []])
    async def test_out_of_scope_list_pipelines_is_denied(self, allowed: list[str]) -> None:
        with (
            patch("modulo.api.mcp_server.validate_current_auth", new=AsyncMock(return_value=True)),
            _node_scope(allowed),
        ):
            result = await list_pipelines_tool()
        assert result == {
            "error": "insufficient_scope",
            "detail": "Tool 'list_pipelines' is outside the node's allowed_tools scope",
        }

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("tool", "handler", "kwargs"),
        _BYPASS_TOOL_CASES,
        ids=[case[0] for case in _BYPASS_TOOL_CASES],
    )
    async def test_every_bypass_tool_denies_when_out_of_node_scope(
        self, tool: str, handler, kwargs: dict[str, object]
    ) -> None:
        """FAR-1976: EVERY pinned bypass tool's node-scope denial arm executes.

        One case per tool in the pinned bypass set, so a tool whose handler
        drops ``_check_node_allowed_tools`` (or whose ``except
        MCPAuthorizationError`` arm is removed) fails here rather than only in
        the changed-lines coverage gate.

        ``_session`` raises if entered, proving the refusal happens on the
        node-scope leg (before any DB work), not as a caught DB error.
        """
        with (
            patch("modulo.api.mcp_server.validate_current_auth", new=AsyncMock(return_value=True)),
            patch("modulo.api.mcp_server._session", side_effect=AssertionError(f"DB reached for {tool}")),
            _node_scope(["create_pipeline"]),
        ):
            result = await handler(**kwargs)
        assert result == {
            "error": "insufficient_scope",
            "detail": f"Tool '{tool}' is outside the node's allowed_tools scope",
        }

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("tool", "handler", "kwargs"),
        _BYPASS_TOOL_CASES,
        ids=[case[0] for case in _BYPASS_TOOL_CASES],
    )
    async def test_every_bypass_tool_allows_with_no_node_scope(
        self, tool: str, handler, kwargs: dict[str, object]
    ) -> None:
        """``None`` (legacy: no node scope) leaves EVERY bypass tool unrestricted.

        The discriminating property is the ABSENCE of the scope refusal on the
        scope leg: with the DB unavailable (``_session`` raises), a tool that
        had wrongly refused would return the ``insufficient_scope`` envelope,
        while an unrestricted tool proceeds past the gate (its later DB error
        surfaces as a generic tool error, never a scope refusal).
        """
        with (
            patch("modulo.api.mcp_server.validate_current_auth", new=AsyncMock(return_value=True)),
            patch("modulo.api.mcp_server._session", side_effect=AssertionError(f"DB reached for {tool}")),
            _node_scope(None),
        ):
            result = await handler(**kwargs)
        assert not (isinstance(result, dict) and result.get("error") == "insufficient_scope")
        assert "insufficient_scope" not in str(result)

    @pytest.mark.asyncio
    async def test_in_scope_search_documentation_is_allowed(self) -> None:
        """The included case for a bypass tool that needs no DB: naming it on
        the node scope lets the real handler return its own result."""
        with (
            patch("modulo.api.mcp_server.validate_current_auth", new=AsyncMock(return_value=True)),
            _node_scope(["search_documentation"]),
        ):
            result = await search_documentation(query="pipeline")
        assert "insufficient_scope" not in result
        assert "count" in result

    @pytest.mark.asyncio
    async def test_in_scope_list_pipelines_is_allowed(self) -> None:
        with (
            patch("modulo.api.mcp_server.validate_current_auth", new=AsyncMock(return_value=True)),
            patch("modulo.api.mcp_server._session", return_value=make_session_context(AsyncMock())),
            patch("modulo.db.crud.pipeline.list_pipelines", new=AsyncMock(return_value=_PAGE)),
            _node_scope(["list_pipelines"]),
        ):
            result = await list_pipelines_tool()
        assert not result["data"]
        assert result["total"] == 0
        assert "error" not in result

    @pytest.mark.asyncio
    async def test_no_node_scope_is_unrestricted(self) -> None:
        """``None`` (legacy: no node scope) leaves the tool unrestricted."""
        with (
            patch("modulo.api.mcp_server.validate_current_auth", new=AsyncMock(return_value=True)),
            patch("modulo.api.mcp_server._session", return_value=make_session_context(AsyncMock())),
            patch("modulo.db.crud.pipeline.list_pipelines", new=AsyncMock(return_value=_PAGE)),
            _node_scope(None),
        ):
            result = await list_pipelines_tool()
        assert not result["data"]
        assert "error" not in result

    @pytest.mark.asyncio
    async def test_denied_tool_never_touches_the_db(self) -> None:
        """The narrowing runs before any DB work: with a session that raises if
        entered, the denial still comes back as insufficient_scope (proving the
        handler returns on the scope leg, not on a caught DB error)."""
        with (
            patch("modulo.api.mcp_server.validate_current_auth", new=AsyncMock(return_value=True)),
            patch("modulo.api.mcp_server._session", side_effect=AssertionError("DB reached")),
            _node_scope(["create_pipeline"]),
        ):
            result = await list_pipelines_tool()
        assert result["error"] == "insufficient_scope"


class TestSnapshotDetailResourceRequiresBothTwins(AuthContext):
    """FAR-1976: the snapshot-detail resource returns a HISTORICAL SNAPSHOT's
    graph (nodes/edges plus connector bindings), so it duplicates BOTH the
    live-graph tool (``get_pipeline_graph``) and the snapshot/diff tool
    (``get_pipeline_diff``). A node must hold BOTH capabilities — either one
    alone is DENIED, which closes the bypass in either direction.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("allowed", "missing"),
        [
            (["get_pipeline_graph"], "get_pipeline_diff"),
            (["get_pipeline_diff"], "get_pipeline_graph"),
        ],
    )
    async def test_either_twin_alone_is_denied(self, allowed: list[str], missing: str) -> None:
        """``_session`` raises if entered, proving the refusal happens on the
        node-scope leg (before any DB work), not as a caught DB error."""
        with (
            patch("modulo.api.mcp_server.validate_current_auth", new=AsyncMock(return_value=True)),
            patch("modulo.api.mcp_server._session", side_effect=AssertionError("DB reached")),
            _node_scope(allowed),
        ):
            result = await resource_pipeline_snapshot_detail(pipeline_id=_PIPELINE_ID, snapshot_id=_SNAPSHOT_ID)
        assert result == f"error: insufficient_scope: Tool '{missing}' is outside the node's allowed_tools scope"

    @pytest.mark.asyncio
    async def test_both_twins_allow_the_read(self) -> None:
        """With both capabilities present the handler reaches the DB layer and
        returns its own result (here: snapshot-not-found), never a refusal."""
        with (
            patch("modulo.api.mcp_server.validate_current_auth", new=AsyncMock(return_value=True)),
            patch("modulo.api.mcp_server._session", return_value=make_session_context(AsyncMock())),
            patch("modulo.api.mcp_server._pipeline_owner_team_id", new=AsyncMock(return_value=None)),
            patch(
                "modulo.db.crud.pipeline_snapshot_versioning.get_snapshot_detail",
                new=AsyncMock(return_value=None),
            ),
            _node_scope(["get_pipeline_graph", "get_pipeline_diff"]),
        ):
            result = await resource_pipeline_snapshot_detail(pipeline_id=_PIPELINE_ID, snapshot_id=_SNAPSHOT_ID)
        assert "insufficient_scope" not in result
        assert result == f"error: Snapshot {_SNAPSHOT_ID} not found"

    @pytest.mark.asyncio
    async def test_no_node_scope_is_unrestricted(self) -> None:
        """``None`` (legacy: no node scope) leaves the resource unrestricted —
        requiring both twins must not turn into requiring either one."""
        with (
            patch("modulo.api.mcp_server.validate_current_auth", new=AsyncMock(return_value=True)),
            patch("modulo.api.mcp_server._session", return_value=make_session_context(AsyncMock())),
            patch("modulo.api.mcp_server._pipeline_owner_team_id", new=AsyncMock(return_value=None)),
            patch(
                "modulo.db.crud.pipeline_snapshot_versioning.get_snapshot_detail",
                new=AsyncMock(return_value=None),
            ),
            _node_scope(None),
        ):
            result = await resource_pipeline_snapshot_detail(pipeline_id=_PIPELINE_ID, snapshot_id=_SNAPSHOT_ID)
        assert "insufficient_scope" not in result
        assert result == f"error: Snapshot {_SNAPSHOT_ID} not found"
