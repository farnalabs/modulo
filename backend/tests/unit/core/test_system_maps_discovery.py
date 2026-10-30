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
    assert first.edge_count == 2
    assert all(edge["config_json"]["suggested"] is True for edge in first.relationships)
    assert {edge["relationship_type"] for edge in first.relationships} == {"trigger", "flow"}
    assert first.skipped == ({"source": "p2", "reason": "improvement loop has no supported non-self relationship"},)


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


def test_connector_sources_become_connector_nodes() -> None:
    draft = discover_draft(
        [
            DiscoverySource(
                kind="connector",
                identifier="c1",
                name="GitHub",
                connector_type_id="github",
            ),
        ]
    )

    assert draft.node_count == 1
    node = draft.nodes[0]
    assert node["node_type"] == "connector"
    assert node["connector_instance_id"] == "c1"
    assert node["config_json"]["connector_type_id"] == "github"


def test_trigger_without_pipeline_binding_is_skipped() -> None:
    draft = discover_draft([DiscoverySource(kind="trigger", identifier="t1", name="manual")])

    assert draft.edge_count == 0
    assert ({"source": "t1", "reason": "trigger has no pipeline binding"}) in draft.skipped


def test_pipeline_folder_missing_from_inventory_is_skipped() -> None:
    draft = discover_draft([DiscoverySource(kind="pipeline", identifier="p1", name="build", folder_id="missing")])

    assert draft.edge_count == 0
    assert ({"source": "p1", "reason": "pipeline folder is not in inventory"}) in draft.skipped


def test_explicit_system_map_tag_skips_improvement_loop_relationship() -> None:
    draft = discover_draft(
        [
            DiscoverySource(kind="pipeline", identifier="p1", name="build", explicit_system_map_tag=True),
        ]
    )

    assert draft.node_count == 1
    assert ({"source": "p1", "reason": "improvement loop has no supported non-self relationship"}) in draft.skipped


def test_empty_inventory_reports_warning_without_skipped_sources() -> None:
    draft = discover_draft([])

    assert draft.node_count == 0
    assert draft.edge_count == 0
    assert draft.skipped == ()
    assert draft.warnings == ("No supported organisation sources were available for discovery",)


def test_supported_sources_without_skips_do_not_warn() -> None:
    draft = discover_draft([DiscoverySource(kind="folder", identifier="f1", name="Delivery", folder_id="f1")])

    assert draft.node_count == 1
    assert draft.skipped == ()
    assert draft.warnings == ()
