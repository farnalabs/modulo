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
from modulo.db.crud.pipeline import ManualNodeOutputSchemaError, enforce_manual_node_output_schemas


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


def _schemaless_manual_node(node_id: str | None = None) -> dict[str, Any]:
    return {"id": node_id or str(uuid.uuid4()), "node_type": "manual", "label": "Gate", "position": {"x": 0, "y": 0}}


def test_schemaless_manual_node_passes_structural_validation() -> None:
    # FAR-2283: structural validation no longer owns the schema requirement;
    # the stored-graph-aware write guard does.
    assert _validate_graph_update([_schemaless_manual_node()], []) is None


def test_stored_schemaless_manual_node_written_back_unchanged_passes() -> None:
    stored = [_schemaless_manual_node()]

    assert enforce_manual_node_output_schemas(copy.deepcopy(stored), previous_nodes=stored) is None


def test_new_schemaless_manual_node_still_rejected() -> None:
    stored = [_schemaless_manual_node()]
    incoming = [*copy.deepcopy(stored), _schemaless_manual_node()]

    with pytest.raises(ManualNodeOutputSchemaError, match="requires an output schema"):
        enforce_manual_node_output_schemas(incoming, previous_nodes=stored)


def test_stripping_stored_schema_still_rejected() -> None:
    stored = _stored_graph()
    stripped = copy.deepcopy(stored)
    for key in ("output_schema_id", "output_schema_pin", "output_schema_json"):
        stripped[0].pop(key, None)

    with pytest.raises(ManualNodeOutputSchemaError, match="requires an output schema"):
        enforce_manual_node_output_schemas(stripped, previous_nodes=stored)


def test_schemaless_manual_node_replacing_non_manual_stored_node_rejected() -> None:
    node_id = str(uuid.uuid4())
    stored = [{"id": node_id, "node_type": "agent", "position": {"x": 0, "y": 0}}]

    with pytest.raises(ManualNodeOutputSchemaError, match="requires an output schema"):
        enforce_manual_node_output_schemas([_schemaless_manual_node(node_id)], previous_nodes=stored)


def test_schemaless_manual_node_without_stored_graph_rejected() -> None:
    with pytest.raises(ManualNodeOutputSchemaError, match="requires an output schema"):
        enforce_manual_node_output_schemas([_schemaless_manual_node()])
