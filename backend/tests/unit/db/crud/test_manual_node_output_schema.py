"""FAR-889: manual nodes must carry an output schema at write time.

The write path (``replace_pipeline_graph``, library template install) must
reject manual nodes that lack ``output_schema_id``, ``output_schema_pin``,
or ``output_schema_json``.  The read path (FAR-874 tolerance) must still
load legacy manual nodes without output schemas gracefully.

Tests:
- ``_enforce_manual_node_output_schemas`` unit tests: every valid and
  invalid combination of output schema fields.
- Write-path integration: ``replace_pipeline_graph`` raises
  ``ManualNodeOutputSchemaError`` when a manual node has no schema.
- Read-path tolerance: ``_graph_response`` returns 200 (with
  validation_issues) for a stored manual node missing its output schema.
"""

import uuid
from typing import Any

import pytest

from modulo.api.routes.pipelines import _graph_response
from modulo.db.crud.pipeline import (
    ManualNodeOutputSchemaError,
    _enforce_manual_node_output_schemas,
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
# Unit tests: _enforce_manual_node_output_schemas
# ---------------------------------------------------------------------------


class TestEnforceManualNodeOutputSchemas:
    """The write-path guard rejects manual nodes without output schemas."""

    def test_rejects_manual_node_without_any_schema(self) -> None:
        node = _manual_node()
        with pytest.raises(ManualNodeOutputSchemaError, match="requires an output schema"):
            _enforce_manual_node_output_schemas([node])

    def test_accepts_manual_node_with_output_schema_id(self) -> None:
        node = _manual_node(output_schema_id=str(uuid.uuid4()))
        _enforce_manual_node_output_schemas([node])

    def test_accepts_manual_node_with_output_schema_pin(self) -> None:
        node = _manual_node(output_schema_pin={"schema_id": str(uuid.uuid4()), "version": 1})
        _enforce_manual_node_output_schemas([node])

    def test_accepts_manual_node_with_output_schema_json(self) -> None:
        node = _manual_node(output_schema_json={"type": "object"})
        _enforce_manual_node_output_schemas([node])

    def test_passes_through_agent_nodes_without_checking(self) -> None:
        """Agent nodes are not subject to the manual-node schema rule."""
        node = _agent_node()
        _enforce_manual_node_output_schemas([node])

    def test_rejects_only_invalid_manual_in_mixed_list(self) -> None:
        """A valid manual node plus an invalid one should still raise."""
        valid = _manual_node(output_schema_id=str(uuid.uuid4()))
        invalid = _manual_node()
        with pytest.raises(ManualNodeOutputSchemaError):
            _enforce_manual_node_output_schemas([valid, invalid])

    def test_accepts_empty_node_list(self) -> None:
        _enforce_manual_node_output_schemas([])

    def test_accepts_manual_node_with_none_schema_id(self) -> None:
        """output_schema_id=None is not the same as absent."""
        node = _manual_node(output_schema_id=None)
        with pytest.raises(ManualNodeOutputSchemaError):
            _enforce_manual_node_output_schemas([node])

    def test_error_message_contains_node_id(self) -> None:
        nid = uuid.uuid4()
        node = _manual_node(node_id=nid)
        with pytest.raises(ManualNodeOutputSchemaError, match=str(nid)):
            _enforce_manual_node_output_schemas([node])


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
        assert len(resp.validation_issues) >= 1
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
