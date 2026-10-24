"""FAR-2089: the ``connector`` node type survives the write -> read round-trip.

The pipeline engine has always supported a ``connector`` node
(``pipeline_engine.graph_cache._make_node_fn`` builds ``make_connector_fn`` for
the type), but the public write model's ``node_type`` ``Literal`` omitted it, so
a GitHub query node could not be authored through the API at all - every graph
carrying one was rejected by FastAPI's request validation before the handler ran.

These tests pin the half of the contract the model owns:

* the type is ACCEPTED with its binding (it was rejected outright before);
* the binding's ``resource`` / ``filters`` / ``data`` survive the write model's
  serialisation (``model_dump(mode="json")``, which is what
  ``_prepare_graph_write`` hands to ``replace_pipeline_graph``) and the stored
  read path (``_stored_node_model``) - the same values, unchanged;
* the save-time rules the new type carries: a ``connector`` node needs a
  binding, and may not reference an agent;
* output-schema pinning is still allowed - a connector's result is an artifact
  like any other node's.

Every assertion here changes if the corresponding code is deleted: the type
tests fail on the Literal, and the round-trip tests fail if ``ConnectorBinding``
stops declaring ``resource`` / ``filters`` / ``data`` (Pydantic's
``extra="ignore"`` silently drops undeclared keys).
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from pydantic import ValidationError

from modulo.api.routes.pipelines import (
    PipelineGraphNode,
    PipelineGraphUpdate,
    _prepare_graph_write,
    _stored_node_model,
)

_NODE_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
_CONNECTOR_ID = uuid.UUID("00000000-0000-0000-0000-0000000000c1")

# A representative GitHub query binding. ``filters`` deliberately carries a
# nested list and a number so a lossy coercion (stringification, list flatten)
# would be visible, not just a missing key.
_RESOURCE = "pull"
_FILTERS: dict[str, Any] = {"state": "open", "labels": ["agent", "backend"], "limit": 25}
_DATA: dict[str, Any] = {"note": "round-trip"}


def _binding() -> dict[str, Any]:
    return {
        "type": "github",
        "instance_id": str(_CONNECTOR_ID),
        "operation": "query",
        "resource": _RESOURCE,
        "filters": _FILTERS,
        "data": _DATA,
    }


def _connector_node(**overrides: Any) -> dict[str, Any]:
    node: dict[str, Any] = {
        "id": str(_NODE_ID),
        "node_type": "connector",
        "position": {"x": 0, "y": 0},
        "connector_binding": _binding(),
    }
    node.update(overrides)
    return node


def test_write_model_accepts_a_connector_node_with_its_binding() -> None:
    """``node_type="connector"`` is a public, authorable node type."""
    node = PipelineGraphNode.model_validate(_connector_node())

    assert node.node_type == "connector"
    assert node.agent_id is None
    assert node.connector_binding is not None
    assert node.connector_binding.resource == _RESOURCE
    assert node.connector_binding.filters == _FILTERS
    assert node.connector_binding.data == _DATA


def test_binding_fields_survive_the_write_dump_and_the_stored_read() -> None:
    """resource/filters/data are byte-identical across write dump -> stored read.

    ``_prepare_graph_write`` is the exact serialisation the REST and PATCH graph
    writes use (``node.model_dump(mode="json")``), and ``_stored_node_model`` is
    the exact parse the graph READ applies to a persisted node.
    """
    update = PipelineGraphUpdate.model_validate({"nodes": [_connector_node()], "edges": []})

    node_data, _edge_data, validator_graph, _bindings = _prepare_graph_write(update)

    persisted_binding = node_data[0]["connector_binding"]
    assert node_data[0]["node_type"] == "connector"
    assert persisted_binding["resource"] == _RESOURCE
    assert persisted_binding["filters"] == _FILTERS
    assert persisted_binding["data"] == _DATA
    # The validator graph carries the same dict, so save-time connector checks
    # see the real query rather than an empty binding.
    assert validator_graph["nodes"][0]["connector_binding"]["resource"] == _RESOURCE

    read_back = _stored_node_model(node_data[0])

    assert read_back.node_type == "connector"
    assert read_back.connector_binding is not None
    assert read_back.connector_binding.resource == _RESOURCE
    assert read_back.connector_binding.filters == _FILTERS
    assert read_back.connector_binding.data == _DATA


def test_a_connector_node_without_a_binding_is_refused() -> None:
    """A binding-less connector node is dead configuration - rejected at save time.

    Without a binding the engine builds the general LLM node function instead of
    ``make_connector_fn``, so the node would run as an agent node with no agent.
    """
    payload = _connector_node(connector_binding=None)

    with pytest.raises(ValidationError) as exc_info:
        PipelineGraphNode.model_validate(payload)

    assert "Connector nodes require a connector_binding" in str(exc_info.value)


def test_a_connector_node_cannot_reference_an_agent() -> None:
    """An agent-bound node never routes its binding, so the pairing is refused."""
    payload = _connector_node(agent_id=str(uuid.uuid4()))

    with pytest.raises(ValidationError) as exc_info:
        PipelineGraphNode.model_validate(payload)

    assert "Connector nodes cannot reference an agent" in str(exc_info.value)


def test_a_connector_node_may_still_pin_its_output_schema() -> None:
    """Output-schema pinning is NOT forbidden: a connector returns an artifact."""
    payload = _connector_node(output_schema_id=str(uuid.uuid4()))

    node = PipelineGraphNode.model_validate(payload)

    assert node.output_schema_id is not None
    assert node.connector_binding is not None
