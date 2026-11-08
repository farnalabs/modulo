"""FAR-1395: connector query authoring on the MCP bind path.

``node_runner._connector_inputs`` reads a binding's ``resource`` / ``filters`` /
``data`` to issue a real connector call. Before this change only the full-graph
write could carry them (through the shared ``ConnectorBinding`` model), so the
dedicated ``bind_connector_to_node`` tool — the surface an operator naturally
reaches for — could bind a connector but never say *what* to fetch, leaving the
run to fall back to ``resource="command"`` and fail inside the connector.

These tests pin the tool contract:

* the three inputs are a PATCH (omitted keys keep their stored value),
* an empty ``resource`` clears the key rather than persisting ``""``,
* a ``connector`` node with a query/write verb must end up with a non-empty
  resource (parity with the REST ``PipelineGraphNode`` model),
* the instance's ``allowed_operations`` are enforced on write with the SAME
  shared helper the run-time ``ConnectorACL`` uses.
"""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.api.mcp_server import (
    _apply_node_connector_binding,
    _validate_binding_arguments,
    bind_connector_to_node,
)

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_CONNECTOR_ID = "11111111-1111-1111-1111-111111111111"


def _pipeline(node: dict) -> SimpleNamespace:
    return SimpleNamespace(graph_nodes_json=[node])


def _connector_node(node_id: uuid.UUID) -> dict:
    return {"id": str(node_id), "node_type": "connector"}


# ---------------------------------------------------------------------------
# _apply_node_connector_binding — the patch contract
# ---------------------------------------------------------------------------


def test_query_inputs_are_patched_onto_the_binding() -> None:
    node_id = uuid.uuid4()
    pipeline = _pipeline(_connector_node(node_id))

    error = _apply_node_connector_binding(
        pipeline,
        node_id,
        str(node_id),
        "github",
        _CONNECTOR_ID,
        operation="query",
        resource="pr_files",
        filters={"owner_repo": "x/y", "pull_number": 7},
        data={"note": "kept"},
    )

    assert error is None
    binding = pipeline.graph_nodes_json[0]["connector_binding"]
    assert binding["operation"] == "query"
    assert binding["resource"] == "pr_files"
    assert binding["filters"] == {"owner_repo": "x/y", "pull_number": 7}
    assert binding["data"] == {"note": "kept"}


def test_omitted_query_inputs_keep_the_stored_values() -> None:
    """A re-bind that names only the verb must not drop the payload (FAR-1141 MAJOR 9)."""
    node_id = uuid.uuid4()
    pipeline = _pipeline(
        {
            **_connector_node(node_id),
            "connector_binding": {
                "type": "github",
                "instance_id": _CONNECTOR_ID,
                "operation": "query",
                "resource": "pr_files",
                "filters": {"pull_number": 7},
            },
        }
    )

    error = _apply_node_connector_binding(
        pipeline,
        node_id,
        str(node_id),
        "github",
        _CONNECTOR_ID,
        operation="query",
    )

    assert error is None
    binding = pipeline.graph_nodes_json[0]["connector_binding"]
    assert binding["resource"] == "pr_files"
    assert binding["filters"] == {"pull_number": 7}


def test_a_blank_resource_clears_the_key() -> None:
    """``""`` reduces the binding to its dispatch shape instead of persisting an empty resource."""
    node_id = uuid.uuid4()
    pipeline = _pipeline(
        {
            **_connector_node(node_id),
            "connector_binding": {
                "type": "github_actions_ci",
                "instance_id": _CONNECTOR_ID,
                "operation": "dispatch",
                "resource": "leftover",
            },
        }
    )

    error = _apply_node_connector_binding(
        pipeline,
        node_id,
        str(node_id),
        "github_actions_ci",
        _CONNECTOR_ID,
        operation="dispatch",
        resource="   ",
    )

    assert error is None
    assert "resource" not in pipeline.graph_nodes_json[0]["connector_binding"]


def test_a_connector_node_query_requires_a_resource() -> None:
    """Parity with the REST model: a query/write connector node needs a non-empty resource."""
    node_id = uuid.uuid4()
    pipeline = _pipeline(_connector_node(node_id))

    error = _apply_node_connector_binding(
        pipeline,
        node_id,
        str(node_id),
        "github",
        _CONNECTOR_ID,
        operation="query",
    )

    assert error is not None
    assert error["error"] == "validation_failed"
    assert error["field"] == "resource"
    assert "non-empty 'resource'" in error["detail"]
    assert "connector_binding" not in pipeline.graph_nodes_json[0]


# ---------------------------------------------------------------------------
# allowed_operations enforcement (FAR-1395)
# ---------------------------------------------------------------------------


def test_a_query_outside_the_allowlist_is_refused() -> None:
    node_id = uuid.uuid4()
    pipeline = _pipeline(_connector_node(node_id))

    error = _apply_node_connector_binding(
        pipeline,
        node_id,
        str(node_id),
        "github",
        _CONNECTOR_ID,
        operation="query",
        resource="pr_files",
        connector_visibility="org",
        connector_allowed_operations=["write"],
        connector_type_id="github",
    )

    assert error is not None
    assert error["error"] == "validation_failed"
    assert error["field"] == "connector_binding"
    assert "'read'" in error["detail"]
    assert "connector_binding" not in pipeline.graph_nodes_json[0]


def test_a_query_inside_the_allowlist_is_accepted() -> None:
    node_id = uuid.uuid4()
    pipeline = _pipeline(_connector_node(node_id))

    error = _apply_node_connector_binding(
        pipeline,
        node_id,
        str(node_id),
        "github",
        _CONNECTOR_ID,
        operation="query",
        resource="pr_files",
        connector_visibility="org",
        connector_allowed_operations=["read"],
        connector_type_id="github",
    )

    assert error is None
    binding = pipeline.graph_nodes_json[0]["connector_binding"]
    assert binding["resource"] == "pr_files"


def test_a_write_outside_the_allowlist_is_refused() -> None:
    node_id = uuid.uuid4()
    pipeline = _pipeline(_connector_node(node_id))

    error = _apply_node_connector_binding(
        pipeline,
        node_id,
        str(node_id),
        "github",
        _CONNECTOR_ID,
        operation="write",
        resource="issue",
        connector_visibility="org",
        connector_allowed_operations=["read"],
        connector_type_id="github",
    )

    assert error is not None
    assert "'write'" in error["detail"]


def test_an_unrestricted_allowlist_accepts_every_verb() -> None:
    node_id = uuid.uuid4()
    pipeline = _pipeline(_connector_node(node_id))

    error = _apply_node_connector_binding(
        pipeline,
        node_id,
        str(node_id),
        "github",
        _CONNECTOR_ID,
        operation="query",
        resource="pr_files",
        connector_visibility="org",
        connector_allowed_operations=[],
        connector_type_id="github",
    )

    assert error is None


# ---------------------------------------------------------------------------
# Unknown operations stay rejected
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"operation": "frobnicate"},
        {"dispatch_action": "explode"},
    ],
)
def test_unknown_operation_vocabulary_is_rejected(kwargs: dict) -> None:
    error = _validate_binding_arguments(kwargs.get("operation"), kwargs.get("dispatch_action"))

    assert error is not None
    assert error["error"] == "validation_failed"


# ---------------------------------------------------------------------------
# bind_connector_to_node — end-to-end through the tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_tool_persists_the_query_payload_and_echoes_it() -> None:
    """The tool writes the payload the engine reads, and reports it back.

    Drives the REAL ``bind_connector_to_node`` with the DB session doubled: the
    assertions are on the node dict that would be flushed, which is exactly the
    shape ``_connector_inputs`` consumes at run start.
    """
    from modulo.api.mcp_server import _ctx_auth_token, _ctx_auth_type, _ctx_org_id, _ctx_role, _ctx_user_id

    pipeline_id = uuid.uuid4()
    node_id = uuid.uuid4()
    connector_id = uuid.UUID(_CONNECTOR_ID)
    _ctx_org_id.set(_ORG_ID)
    _ctx_role.set("operator")
    _ctx_user_id.set(uuid.uuid4())
    _ctx_auth_token.set("mk_testprefix_testsecretkey1234567890abc")
    _ctx_auth_type.set("api_key")

    connector = MagicMock(
        id=connector_id,
        organisation_id=_ORG_ID,
        name="gh",
        visibility="org",
        owner_team_id=None,
        connector_type_id="github",
        allowed_operations=["read"],
    )
    pipeline = SimpleNamespace(
        id=pipeline_id,
        owner_team_id=None,
        graph_nodes_json=[_connector_node(node_id)],
    )
    session = AsyncMock()
    session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=lambda: pipeline))
    session_scope = MagicMock()
    session_scope.__aenter__ = AsyncMock(return_value=session)
    session_scope.__aexit__ = AsyncMock(return_value=False)

    try:
        with (
            patch("modulo.api.mcp_server.validate_current_auth", return_value=True),
            patch("modulo.api.mcp_server._session", return_value=session_scope),
            patch("modulo.db.crud.connector_instance.get_connector_instance", new=AsyncMock(return_value=connector)),
            patch(
                "modulo.db.crud.pipeline._graph_content_unchanged",
                return_value=True,
            ),
        ):
            result = await bind_connector_to_node(
                pipeline_id=str(pipeline_id),
                node_id=str(node_id),
                connector_type="github",
                connector_instance_id=_CONNECTOR_ID,
                operation="query",
                resource="pr_files",
                filters={"owner_repo": "x/y", "pull_number": 7},
                data={"note": "round-trip"},
            )
    finally:
        _ctx_org_id.set(None)
        _ctx_role.set(None)
        _ctx_user_id.set(None)
        _ctx_auth_token.set(None)
        _ctx_auth_type.set(None)

    assert result["status"] == "bound", result
    binding = pipeline.graph_nodes_json[0]["connector_binding"]
    assert binding["resource"] == "pr_files"
    assert binding["filters"] == {"owner_repo": "x/y", "pull_number": 7}
    assert binding["data"] == {"note": "round-trip"}
    assert result["resource"] == "pr_files"
    assert result["filters"] == {"owner_repo": "x/y", "pull_number": 7}
    assert result["data"] == {"note": "round-trip"}
