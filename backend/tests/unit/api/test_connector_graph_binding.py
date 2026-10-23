import uuid

from modulo.api.routes.pipelines import PipelineGraphNode


def test_connector_node_accepts_query_payload() -> None:
    connector_id = uuid.uuid4()
    node = PipelineGraphNode.model_validate(
        {
            "id": uuid.uuid4(),
            "node_type": "connector",
            "position": {"x": 0, "y": 0},
            "connector_binding": {
                "type": "github",
                "instance_id": connector_id,
                "operation": "query",
                "resource": "pull",
                "filters": {"repo": "farnalabs/modulo", "pull_number": "2213"},
                "data": {"include_files": True},
            },
        }
    )

    binding = node.model_dump(mode="json")["connector_binding"]
    assert binding["resource"] == "pull"
    assert binding["filters"]["repo"] == "farnalabs/modulo"
    assert binding["data"]["include_files"] is True
