"""Unit tests for category validator."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

from modulo.core.graph_validator import GraphValidator
from modulo.core.graph_validator.category_validator import validate_node_categories

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_category(
    cat_id: uuid.UUID | None = None,
    *,
    name: str = "test-category",
) -> MagicMock:
    c = MagicMock()
    c.id = cat_id or uuid.uuid4()
    c.name = name
    return c


def _node(
    *,
    nid: str = "n1",
    node_type: str = "agent",
    category_id: str | None = None,
) -> dict:
    node = {"id": nid, "node_type": node_type}
    if category_id is not None:
        node["node_category_id"] = category_id
    return node


def _mock_session() -> AsyncMock:
    return AsyncMock()


def _session_with_category(category: MagicMock | None) -> AsyncMock:
    session = _mock_session()
    scalars_result = MagicMock()
    scalars_result.all.return_value = [category] if category else []
    scalars_result.scalar_one_or_none.return_value = category
    execute_result = MagicMock()
    execute_result.scalars.return_value = scalars_result
    session.execute = AsyncMock(return_value=execute_result)
    return session


# ---------------------------------------------------------------------------
# validate_node_categories — no references
# ---------------------------------------------------------------------------


async def test_no_category_refs_returns_valid():
    graph = {"nodes": [_node(nid="n1")], "edges": []}
    session = _mock_session()
    result = await validate_node_categories(graph, session)
    assert result.is_valid
    assert not result.issues
    session.execute.assert_not_called()


async def test_empty_nodes_returns_valid():
    graph = {"nodes": [], "edges": []}
    session = _mock_session()
    result = await validate_node_categories(graph, session)
    assert result.is_valid


# ---------------------------------------------------------------------------
# Category reference — valid and invalid
# ---------------------------------------------------------------------------


async def test_valid_category_ref_is_accepted():
    cat_id = uuid.uuid4()
    category = _mock_category(cat_id, name="analysis")
    graph = {
        "nodes": [_node(nid="n1", category_id=str(cat_id))],
        "edges": [],
    }
    session = _session_with_category(category)
    result = await validate_node_categories(graph, session)
    assert result.is_valid
    assert not result.issues


async def test_multiple_nodes_same_category():
    cat_id = uuid.uuid4()
    category = _mock_category(cat_id, name="analysis")
    graph = {
        "nodes": [
            _node(nid="n1", category_id=str(cat_id)),
            _node(nid="n2", category_id=str(cat_id)),
        ],
        "edges": [],
    }
    session = _session_with_category(category)
    result = await validate_node_categories(graph, session)
    assert result.is_valid


async def test_missing_category_is_error():
    cat_id = uuid.uuid4()
    graph = {
        "nodes": [_node(nid="n1", category_id=str(cat_id))],
        "edges": [],
    }
    session = _session_with_category(None)
    result = await validate_node_categories(graph, session)
    assert not result.is_valid
    assert any(i.code == "CATEGORY_NOT_FOUND" for i in result.issues)
    assert any(str(cat_id) in i.message for i in result.issues)


async def test_missing_category_reports_node_id():
    cat_id = uuid.uuid4()
    graph = {
        "nodes": [_node(nid="my-node", category_id=str(cat_id))],
        "edges": [],
    }
    session = _session_with_category(None)
    result = await validate_node_categories(graph, session)
    matching = [i for i in result.issues if i.code == "CATEGORY_NOT_FOUND"]
    assert matching
    assert matching[0].node_id == "my-node"


# ---------------------------------------------------------------------------
# Invalid category IDs
# ---------------------------------------------------------------------------


async def test_invalid_uuid_in_category_id_is_error():
    graph = {
        "nodes": [_node(nid="n1", category_id="not-a-uuid")],
        "edges": [],
    }
    session = _mock_session()
    result = await validate_node_categories(graph, session)
    assert not result.is_valid
    assert any(i.code == "CATEGORY_INVALID_ID" for i in result.issues)


async def test_mixed_valid_and_invalid_ids():
    valid_id = uuid.uuid4()
    category = _mock_category(valid_id, name="valid")
    graph = {
        "nodes": [
            _node(nid="n1", category_id=str(valid_id)),
            _node(nid="n2", category_id="bad-id"),
        ],
        "edges": [],
    }
    session = _session_with_category(category)
    result = await validate_node_categories(graph, session)
    assert not result.is_valid
    codes = {i.code for i in result.issues}
    assert "CATEGORY_INVALID_ID" in codes
    # n2 gets CATEGORY_INVALID_ID — no DB lookup for unparseable IDs
    assert "CATEGORY_NOT_FOUND" not in codes


# ---------------------------------------------------------------------------
# GraphValidator integration
# ---------------------------------------------------------------------------


async def test_validator_passes_through_graph_validator():
    """GraphValidator.validate() should run the category check."""
    cat_id = uuid.uuid4()
    category = _mock_category(cat_id, name="analysis")
    graph = {
        "nodes": [_node(nid="n1", category_id=str(cat_id))],
        "edges": [],
    }

    session = _session_with_category(category)

    snap = MagicMock()
    snap.graph_json = graph
    snap.schema_pins_json = []
    snap.connector_bindings_json = []
    snap.model_backend_pins_json = []
    snap.environment_profile_id = None

    validator = GraphValidator()
    result = await validator.validate(snap, session)
    assert result.is_valid


async def test_validator_returns_category_error():
    """GraphValidator.validate() should surface category errors."""
    cat_id = uuid.uuid4()
    graph = {
        "nodes": [_node(nid="n1", category_id=str(cat_id))],
        "edges": [],
    }

    session = _session_with_category(None)

    snap = MagicMock()
    snap.graph_json = graph
    snap.schema_pins_json = []
    snap.connector_bindings_json = []
    snap.model_backend_pins_json = []
    snap.environment_profile_id = None

    validator = GraphValidator()
    result = await validator.validate(snap, session)
    assert not result.is_valid
    assert any(i.code == "CATEGORY_NOT_FOUND" for i in result.issues)


async def test_validator_category_check_in_validate_for_run():
    """GraphValidator.validate_for_run() should also run category check."""
    cat_id = uuid.uuid4()
    graph = {
        "nodes": [_node(nid="n1", category_id=str(cat_id))],
        "edges": [],
    }

    session = _session_with_category(None)

    snap = MagicMock()
    snap.graph_json = graph
    snap.schema_pins_json = []
    snap.connector_bindings_json = []
    snap.model_backend_pins_json = []
    snap.environment_profile_id = None

    validator = GraphValidator()
    result = await validator.validate_for_run(snap, {}, session)
    assert not result.is_valid
    assert any(i.code == "CATEGORY_NOT_FOUND" for i in result.issues)


async def test_category_check_does_not_block_topology_errors():
    """Category errors should not prevent topology checks from running."""
    graph = {
        "nodes": [],
        "edges": [],
    }

    session = _session_with_category(None)

    snap = MagicMock()
    snap.graph_json = graph
    snap.schema_pins_json = []
    snap.connector_bindings_json = []
    snap.model_backend_pins_json = []
    snap.environment_profile_id = None

    validator = GraphValidator()
    result = await validator.validate(snap, session)
    assert not result.is_valid
    assert any(i.code == "TOPOLOGY_NO_NODES" for i in result.issues)
