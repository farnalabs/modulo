from modulo.core.system_maps_discovery import DiscoverySource, discover_draft


def test_discovery_is_deterministic_and_marks_every_edge_suggested() -> None:
    sources = [
        DiscoverySource(kind="pipeline", identifier="p2", name="improve-tests"),
        DiscoverySource(kind="pipeline", identifier="p1", name="build", folder_id="f1"),
        DiscoverySource(kind="folder", identifier="f1", name="Delivery"),
        DiscoverySource(kind="trigger", identifier="t1", name="push", pipeline_id="p1", trigger_type="webhook"),
    ]

    first = discover_draft(sources)
    second = discover_draft(reversed(sources))

    assert first == second
    assert first.edge_count == 3
    assert all(edge["config_json"]["suggested"] is True for edge in first.relationships)
    assert {edge["relationship_type"] for edge in first.relationships} == {"trigger", "flow", "loop_back"}


def test_unsupported_and_ambiguous_sources_are_reported() -> None:
    draft = discover_draft(
        [
            DiscoverySource(kind="unknown", identifier="x", name="Unknown"),
            DiscoverySource(kind="trigger", identifier="t", name="orphan", pipeline_id="missing"),
        ]
    )

    assert draft.node_count == 1
    assert draft.skipped
    assert draft.warnings
