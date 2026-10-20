"""FAR-1976: node ``allowed_tools`` narrowing on the tool-backed MCP resources.

The tool handlers were narrowed in the previous commit, but ``resources/read``
reached the same tenant data through 13 ``@mcp.resource`` handlers that the
tools' gate never saw. Each tool-backed resource now applies the SAME node
``allowed_tools`` leg, gating under the duplicated tool's name
(``modulo://pipelines`` gates as ``list_pipelines``, and so on).

These tests drive the REAL resource handler path (no mock of the gate) so a
removed helper call fails here:

* a node whose ``allowed_tools`` excludes the duplicated tool is DENIED;
* a node whose ``allowed_tools`` includes it is ALLOWED;
* no node scope (``None``) is unrestricted/legacy.

The mirrored structural guard lives in
``tests/unit/test_mcp_structural_coverage.py``.
"""

import uuid
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from modulo.api.mcp_server import (
    _ctx_node_allowed_tools,
    resource_connectors,
    resource_hitl_review,
    resource_library,
    resource_library_detail,
    resource_model_backends,
    resource_pipeline_detail,
    resource_pipeline_runs,
    resource_pipeline_snapshot_detail,
    resource_pipelines,
    resource_run,
    resource_schema_detail,
    resource_schemas,
)
from tests.unit.mcp.helpers import AuthContext, make_session_context

# A pipeline list page shaped like ``modulo.db.crud.base.PageResult``.
_PAGE = SimpleNamespace(items=[], total=0, next_cursor=None, has_more=False)
# A library page shaped like ``modulo.core.library_service``'s result.
_LIBRARY_PAGE = SimpleNamespace(items=[], total=0, next_cursor=None, has_more=False)


@contextmanager
def _node_scope(tools: list[str] | None):
    """Set + restore the request-scoped node allowed_tools ContextVar."""
    token = _ctx_node_allowed_tools.set(tools)
    try:
        yield
    finally:
        _ctx_node_allowed_tools.reset(token)


#: Every tool-backed resource: URI, the duplicated tool it must gate under,
#: the real handler, and the arguments needed to invoke it.
_RESOURCE_CASES = [
    ("modulo://pipelines", "list_pipelines", resource_pipelines, {}),
    (
        "modulo://pipelines/{pipeline_id}",
        "list_pipelines",
        resource_pipeline_detail,
        {"pipeline_id": str(uuid.uuid4())},
    ),
    (
        "modulo://pipelines/{pipeline_id}/runs",
        "list_runs",
        resource_pipeline_runs,
        {"pipeline_id": str(uuid.uuid4())},
    ),
    (
        "modulo://pipelines/{pipeline_id}/snapshots/{snapshot_id}",
        "get_pipeline_graph",
        resource_pipeline_snapshot_detail,
        {"pipeline_id": str(uuid.uuid4()), "snapshot_id": str(uuid.uuid4())},
    ),
    ("modulo://runs/{run_id}", "get_run_status", resource_run, {"run_id": str(uuid.uuid4())}),
    (
        "modulo://runs/{run_id}/hitl/{review_id}",
        "get_hitl_review",
        resource_hitl_review,
        {"run_id": str(uuid.uuid4()), "review_id": "review-1"},
    ),
    ("modulo://schemas", "list_schemas", resource_schemas, {}),
    (
        "modulo://schemas/{schema_id}@{version}",
        "get_schema_versions",
        resource_schema_detail,
        {"schema_id": str(uuid.uuid4()), "version": "latest"},
    ),
    ("modulo://connectors", "list_connectors", resource_connectors, {}),
    ("modulo://model-backends", "list_model_backends", resource_model_backends, {}),
    ("modulo://library", "search_library", resource_library, {}),
    (
        "modulo://library/{primitive_type}/{slug}",
        "search_library",
        resource_library_detail,
        {"primitive_type": "agent", "slug": "example"},
    ),
]


class TestResourceNodeScopeEnforcement(AuthContext):
    """Real resource handler path: the node allowed_tools leg is enforced."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("uri", "tool", "handler", "kwargs"),
        _RESOURCE_CASES,
        ids=[case[0] for case in _RESOURCE_CASES],
    )
    async def test_out_of_scope_resource_read_is_denied(
        self, uri: str, tool: str, handler, kwargs: dict[str, str]
    ) -> None:
        """A node scope excluding the duplicated tool denies the resource read.

        ``_session`` raises if entered, proving the refusal happens on the
        node-scope leg (before any DB work), not as a caught DB error.
        """
        with (
            patch("modulo.api.mcp_server.validate_current_auth", new=AsyncMock(return_value=True)),
            patch("modulo.api.mcp_server._session", side_effect=AssertionError(f"DB reached for {uri}")),
            _node_scope(["create_pipeline"]),
        ):
            result = await handler(**kwargs)
        assert result == (f"error: insufficient_scope: Tool '{tool}' is outside the node's allowed_tools scope")

    @pytest.mark.asyncio
    async def test_in_scope_pipelines_resource_is_allowed(self) -> None:
        with (
            patch("modulo.api.mcp_server.validate_current_auth", new=AsyncMock(return_value=True)),
            patch("modulo.api.mcp_server._session", return_value=make_session_context(AsyncMock())),
            patch("modulo.db.crud.pipeline.list_pipelines", new=AsyncMock(return_value=_PAGE)),
            _node_scope(["list_pipelines"]),
        ):
            result = await resource_pipelines()
        assert result == "Pipelines (0 total):\n"
        assert "insufficient_scope" not in result

    @pytest.mark.asyncio
    async def test_in_scope_library_resource_is_allowed(self) -> None:
        with (
            patch("modulo.api.mcp_server.validate_current_auth", new=AsyncMock(return_value=True)),
            patch("modulo.api.mcp_server._session", return_value=make_session_context(AsyncMock())),
            patch("modulo.api.mcp_server.list_primitives", new=AsyncMock(return_value=_LIBRARY_PAGE)),
            _node_scope(["search_library"]),
        ):
            result = await resource_library()
        assert result == "Library is empty."
        assert "insufficient_scope" not in result

    @pytest.mark.asyncio
    async def test_no_node_scope_is_unrestricted(self) -> None:
        """``None`` (legacy: no node scope) leaves the resource unrestricted."""
        with (
            patch("modulo.api.mcp_server.validate_current_auth", new=AsyncMock(return_value=True)),
            patch("modulo.api.mcp_server._session", return_value=make_session_context(AsyncMock())),
            patch("modulo.db.crud.pipeline.list_pipelines", new=AsyncMock(return_value=_PAGE)),
            _node_scope(None),
        ):
            result = await resource_pipelines()
        assert result == "Pipelines (0 total):\n"
        assert "insufficient_scope" not in result
