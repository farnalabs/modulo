"""Integration contract tests for the discovery graph shape.

The database round-trip is covered by the existing lifecycle-map integration
fixtures on branches where FAR-2236 has not landed yet; this test keeps the
cross-layer contract executable without inventing a second schema.
"""

import pytest

from modulo.core.lifecycle_map.validation import normalize_content
from modulo.core.system_maps_discovery import DiscoverySource, discover_draft

pytestmark = pytest.mark.integration


def test_discovery_draft_contains_only_suggested_edges() -> None:
    draft = discover_draft(
        [
            DiscoverySource(kind="pipeline", identifier="p", name="build"),
            DiscoverySource(kind="trigger", identifier="t", name="webhook", pipeline_id="p"),
        ]
    )
    assert all(edge["config_json"].get("suggested") is True for edge in draft.relationships)


def test_discovery_content_round_trips_through_lifecycle_map_validation() -> None:
    draft = discover_draft(
        [
            DiscoverySource(
                kind="pipeline",
                identifier="00000000-0000-0000-0000-000000000001",
                name="improve-tests",
            ),
        ]
    )
    content = {
        "stages": [
            {
                "id": node["node_key"],
                "name": node["display_name"],
                "type": "modulo" if node["node_type"] == "pipeline" else "placeholder",
                "pipeline_id": node.get("pipeline_id"),
            }
            for node in draft.nodes
        ],
        "edges": [
            {
                "id": f"{edge['source_node_key']}->{edge['target_node_key']}:{edge['relationship_type']}",
                "source": edge["source_node_key"],
                "target": edge["target_node_key"],
                "config_json": edge["config_json"],
            }
            for edge in draft.relationships
        ],
        "notes": "discovery",
    }
    assert normalize_content(content) == content
    assert draft.edge_count == 0
    assert draft.skipped
