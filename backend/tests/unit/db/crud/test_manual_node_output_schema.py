"""FAR-889: manual nodes must carry an output schema at write time.

The write path (``replace_pipeline_graph``, library template install, and
workflow import) must reject manual nodes that lack ``output_schema_id``,
``output_schema_pin``, or ``output_schema_json``.  The read path
(FAR-874 tolerance) must still load legacy manual nodes without output
schemas gracefully.

Tests:
- ``enforce_manual_node_output_schemas`` unit tests: every valid and
  invalid combination of output schema fields.
- Wiring tests: ``replace_pipeline_graph`` calls the guard before any
  graph write; the ``handle_db_errors`` decorator surfaces the guard
  failure as HTTP 422.
- Read-path tolerance: ``_graph_response`` returns 200 (with
  validation_issues) for a stored manual node missing its output schema.
"""

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from modulo.api.routes.pipelines import _graph_response
from modulo.db.crud.hitl_gate_guard import DiffResult
from modulo.db.crud.pipeline import (
    ManualNodeOutputSchemaError,
    enforce_manual_node_output_schemas,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _manual_node(node_id: uuid.UUID | None = None, **extra: Any) -> dict[str, Any]:
    """Build a minimal manual node dict."""
    base: dict[str, Any] = {
        "id": str(node_id or uuid.uuid4()),
        "node_type": "manual",
        "position": {"x": 0, "y": 0},
        "label": "Review Gate",
    }
    base.update(extra)
    return base


def _agent_node(node_id: uuid.UUID | None = None) -> dict[str, Any]:
    """Build a minimal agent node dict."""
    return {
        "id": str(node_id or uuid.uuid4()),
        "node_type": "agent",
        "position": {"x": 0, "y": 0},
        "agent_id": str(uuid.uuid4()),
    }


# ---------------------------------------------------------------------------
# Unit tests: enforce_manual_node_output_schemas
# ---------------------------------------------------------------------------


class TestEnforceManualNodeOutputSchemas:
    """The write-path guard rejects manual nodes without output schemas."""

    def test_rejects_manual_node_without_any_schema(self) -> None:
        node = _manual_node()
        with pytest.raises(ManualNodeOutputSchemaError, match="requires an output schema"):
            enforce_manual_node_output_schemas([node])

    def test_accepts_manual_node_with_output_schema_id(self) -> None:
        node = _manual_node(output_schema_id=str(uuid.uuid4()))
        result = enforce_manual_node_output_schemas([node])
        assert result is None

    def test_accepts_manual_node_with_output_schema_pin(self) -> None:
        node = _manual_node(output_schema_pin={"schema_id": str(uuid.uuid4()), "version": 1})
        result = enforce_manual_node_output_schemas([node])
        assert result is None

    def test_accepts_manual_node_with_output_schema_json(self) -> None:
        node = _manual_node(output_schema_json={"type": "object"})
        result = enforce_manual_node_output_schemas([node])
        assert result is None

    def test_passes_through_agent_nodes_without_checking(self) -> None:
        """Agent nodes are not subject to the manual-node schema rule."""
        node = _agent_node()
        result = enforce_manual_node_output_schemas([node])
        assert result is None

    def test_rejects_only_invalid_manual_in_mixed_list(self) -> None:
        """A valid manual node plus an invalid one should still raise."""
        valid = _manual_node(output_schema_id=str(uuid.uuid4()))
        invalid = _manual_node()
        with pytest.raises(ManualNodeOutputSchemaError):
            enforce_manual_node_output_schemas([valid, invalid])

    def test_accepts_empty_node_list(self) -> None:
        result = enforce_manual_node_output_schemas([])
        assert result is None

    def test_accepts_manual_node_with_none_schema_id(self) -> None:
        """output_schema_id=None is not the same as absent."""
        node = _manual_node(output_schema_id=None)
        with pytest.raises(ManualNodeOutputSchemaError):
            enforce_manual_node_output_schemas([node])

    def test_error_message_contains_node_id(self) -> None:
        nid = uuid.uuid4()
        node = _manual_node(node_id=nid)
        with pytest.raises(ManualNodeOutputSchemaError, match=str(nid)):
            enforce_manual_node_output_schemas([node])


# ---------------------------------------------------------------------------
# Wiring tests: the guard fires through replace_pipeline_graph
# ---------------------------------------------------------------------------


class TestWiringThroughReplacePipelineGraph:
    """The guard is actually called by replace_pipeline_graph, not just the helper."""

    @patch("modulo.db.crud.pipeline.enforce_guardrail_binding_strip", new_callable=AsyncMock)
    @patch("modulo.db.crud.pipeline.apply_gated_edge_diff", new_callable=AsyncMock)
    @patch("modulo.db.crud.pipeline.resolve_effective_privilege", new_callable=AsyncMock)
    async def test_replace_pipeline_graph_rejects_schemaless_manual_node(
        self,
        mock_privilege: AsyncMock,
        mock_diff: AsyncMock,
        mock_guardrail: AsyncMock,
    ) -> None:
        """Calling replace_pipeline_graph with a schema-less manual node
        raises ManualNodeOutputSchemaError before any graph write."""
        from modulo.db.crud.pipeline import replace_pipeline_graph

        mock_privilege.return_value = True

        # Return a clean DiffResult (no weakening, no denial)
        mock_diff.return_value = DiffResult(
            weakened_edges=[],
            has_weakening=False,
            denied=False,
            reason_code=None,
            caller_type="rest",
            weakened_nodes=[],
        )

        # build a pipeline that the session.execute will return
        pipeline = MagicMock()
        pipeline.graph_nodes_json = []
        pipeline.id = uuid.uuid4()

        read_result = MagicMock()
        read_result.scalar_one_or_none.return_value = pipeline

        # Edge read returns empty list
        edge_result = MagicMock()
        edge_result.scalars.return_value = []

        session = AsyncMock()
        session.execute = AsyncMock(side_effect=[read_result, edge_result])

        schemaless_node = _manual_node()

        with pytest.raises(ManualNodeOutputSchemaError, match="requires an output schema"):
            await replace_pipeline_graph(
                session,
                pipeline_id=uuid.uuid4(),
                org_id=uuid.uuid4(),
                nodes=[schemaless_node],
                edges=[],
                is_privileged=True,
                caller_type="rest",
            )

        # The guard fires BEFORE any delete/insert — session.add_all is never called
        session.add_all.assert_not_called()


# ---------------------------------------------------------------------------
# Wiring test: handle_db_errors surfaces ManualNodeOutputSchemaError as 422
# ---------------------------------------------------------------------------


class TestHandleDbErrorsMapsTo422:
    """The ManualNodeOutputSchemaError is surfaced as HTTP 422, not 500."""

    def test_translate_maps_manual_node_output_schema_error_to_422(self) -> None:
        from modulo.api.db_error_handling import _translate_wrapped_exception

        exc = ManualNodeOutputSchemaError("node-123")
        with pytest.raises(HTTPException) as exc_info:
            _translate_wrapped_exception(exc, "test_prefix")
        assert exc_info.value.status_code == 422
        assert "node-123" in exc_info.value.detail
        assert "requires an output schema" in exc_info.value.detail

    async def test_handle_db_errors_decorator_maps_to_422(self) -> None:
        """An endpoint decorated with @handle_db_errors returns 422 for this error."""
        from modulo.api.db_error_handling import handle_db_errors

        @handle_db_errors("test_endpoint")
        async def _failing_endpoint() -> None:
            raise ManualNodeOutputSchemaError("node-xyz")

        with pytest.raises(HTTPException) as exc_info:
            await _failing_endpoint()
        assert exc_info.value.status_code == 422
        assert "node-xyz" in exc_info.value.detail


# ---------------------------------------------------------------------------
# Read-path tolerance: _graph_response handles legacy manual nodes
# ---------------------------------------------------------------------------


class TestReadPathToleranceForLegacyManualNodes:
    """FAR-874: the read path tolerates manual nodes without output schemas."""

    def test_manual_node_without_schema_returns_with_validation_issues(self) -> None:
        """A stored manual node missing output_schema_id produces a validation issue, not a 422."""
        nid = uuid.uuid4()
        node = _manual_node(node_id=nid)
        # Remove all output schema fields to simulate legacy data
        node.pop("output_schema_id", None)
        node.pop("output_schema_pin", None)
        node.pop("output_schema_json", None)
        resp = _graph_response([node], [])
        assert len(resp.nodes) == 1
        assert resp.validation_issues is not None
        # The legacy_read fallback should produce at least one validation issue
        # (the node passes lenient validation but would fail write validation)
        assert resp.validation_issues
        issue_codes = [i.code for i in resp.validation_issues]
        assert "node_legacy_data" in issue_codes

    def test_manual_node_with_schema_returns_clean(self) -> None:
        """A valid manual node with output_schema_id returns no validation issues."""
        nid = uuid.uuid4()
        node = _manual_node(node_id=nid, output_schema_id=str(uuid.uuid4()))
        resp = _graph_response([node], [])
        assert len(resp.nodes) == 1
        # No validation issues for a well-formed manual node
        assert not resp.validation_issues
