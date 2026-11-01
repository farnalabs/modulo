"""FAR-2278: a manual node read via get_pipeline_graph must be writable back.

Library/template manual gates carry only an inline ``output_schema_json``.
The write-path guard accepts that form, so the MCP/REST structural validator
must too, otherwise read -> write of an unchanged graph fails.
"""

import copy
import uuid
from typing import Any

import pytest

from modulo.api.mcp_server import _validate_graph_update
from modulo.api.middleware.sensitive_mask import mask_pipeline_graph_node
from modulo.core.library_service._seed_data import _PR_TEMPLATE_NODES
from modulo.db.crud.pipeline import enforce_manual_node_output_schemas


def _stored_graph() -> list[dict[str, Any]]:
    gate = copy.deepcopy(next(n for n in _PR_TEMPLATE_NODES if n["node_type"] == "manual"))
    gate["id"] = str(uuid.uuid4())
    return [gate]


def test_manual_node_with_inline_schema_round_trips_unchanged() -> None:
    stored = _stored_graph()
    read_back = [mask_pipeline_graph_node(n) for n in stored]

    assert _validate_graph_update(read_back, []) is None
    enforce_manual_node_output_schemas(read_back)


@pytest.mark.parametrize(
    "schema_field",
    [
        {"output_schema_id": str(uuid.uuid4())},
        {"output_schema_json": {"type": "object"}},
    ],
)
def test_manual_node_schema_forms_accepted(schema_field: dict[str, Any]) -> None:
    node = {"id": str(uuid.uuid4()), "node_type": "manual", "label": "Gate", "position": {"x": 0, "y": 0}}
    node.update(schema_field)

    assert _validate_graph_update([node], []) is None


def test_manual_node_without_any_schema_still_rejected() -> None:
    node = {"id": str(uuid.uuid4()), "node_type": "manual", "label": "Gate", "position": {"x": 0, "y": 0}}

    result = _validate_graph_update([node], [])

    assert result is not None
    assert "Manual nodes require an output schema" in result["detail"]
