"""Integration contract tests for the discovery graph shape.

The database round-trip is covered by the existing lifecycle-map integration
fixtures on branches where FAR-2236 has not landed yet; this test keeps the
cross-layer contract executable without inventing a second schema.
"""

from modulo.core.system_maps_discovery import DiscoverySource, discover_draft


def test_discovery_draft_contains_only_suggested_edges() -> None:
    draft = discover_draft(
        [
            DiscoverySource(kind="pipeline", identifier="p", name="build"),
            DiscoverySource(kind="trigger", identifier="t", name="webhook", pipeline_id="p"),
        ]
    )
    assert all(edge["config_json"].get("suggested") is True for edge in draft.relationships)
