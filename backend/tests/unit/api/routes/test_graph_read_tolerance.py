"""FAR-874: GET /pipelines/{id}/graph must never 422 on a read.

The graph READ path previously re-validated every stored node through strict
Pydantic models (``PipelineGraphNode.model_validate``).  If any node carried
data that a later model revision made stricter (e.g. a composite node whose
``composite_ref`` was removed, or an agent node whose bound ``Agent`` was
deleted), the entire read failed with HTTP 422 — making the graph unreadable
and therefore uneditable via the API / MCP path.

The fix makes the read path **permissive**: each node and edge is validated
individually with ``context={"legacy_read": True}``, and validation failures
are collected as ``validation_issues`` in the response instead of raising 422.

Tests:
- ``_graph_response`` unit tests: stored data that fails the current Pydantic
  schema is returned with warnings, never a 422.
- Endpoint integration tests: the GET /graph endpoint returns 200 even when
  the DB contains a node that fails validation.
- Regression: a parameterised test over every node_type + edge_type + composite
  shape confirms the read path tolerates each shape.
"""

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from tests.unit.api.mock_session import configure_mock_session

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context, get_settings
from modulo.api.main import app
from modulo.api.routes.pipelines import (
    GraphValidationIssue,
    _graph_response,
)
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_PIPELINE_ID = uuid.uuid4()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


def _minimal_node(node_id: uuid.UUID, node_type: str = "agent", **extra: Any) -> dict[str, Any]:
    """Build a minimal valid node dict for the given node_type."""
    base: dict[str, Any] = {
        "id": str(node_id),
        "node_type": node_type,
        "position": {"x": 0, "y": 0},
    }
    if node_type == "agent":
        base["agent_id"] = str(uuid.uuid4())
    elif node_type == "sandbox_agent":
        base["template_id"] = "opencode"
        base["agent_prompt"] = "test"
        base["agent_commands"] = ["echo hello"]
    elif node_type == "composite":
        base["composite_ref"] = str(uuid.uuid4())
    elif node_type == "manual":
        base["label"] = "Manual step"
        base["output_schema_id"] = str(uuid.uuid4())
    elif node_type == "router":
        base["router_config"] = {"rules": [{"target": str(uuid.uuid4())}]}
    elif node_type == "hitl":
        base["hitl_config"] = {
            "label": "Review",
            "description": "Human review gate",
            "claim_expiry_minutes": 60,
        }
    elif node_type == "join":
        base["collect"] = [{"node": str(uuid.uuid4())}]
        base["aggregate"] = {"kind": "merge_by_key", "key": "result"}
    base.update(extra)
    return base


def _minimal_edge(
    source_id: uuid.UUID,
    target_id: uuid.UUID,
    edge_type: str = "normal",
) -> dict[str, Any]:
    return {
        "id": str(uuid.uuid4()),
        "source_node_id": str(source_id),
        "target_node_id": str(target_id),
        "edge_type": edge_type,
    }


# ---------------------------------------------------------------------------
# Unit tests: _graph_response
# ---------------------------------------------------------------------------


class TestGraphResponseNever422:
    """_graph_response must never raise HTTPException on read."""

    def test_valid_graph_passes(self) -> None:
        nid = uuid.uuid4()
        nodes = [_minimal_node(nid)]
        edges: list[dict[str, Any]] = []
        resp = _graph_response(nodes, edges)
        assert len(resp.nodes) == 1
        assert resp.nodes[0].id == nid
        assert not resp.validation_issues

    def test_composite_without_ref_returns_with_warning(self) -> None:
        """A composite node missing composite_ref fails the write-path
        validator but the read path must return it with a warning."""
        nid = uuid.uuid4()
        # composite without composite_ref — would fail _validate_composite_node
        nodes = [
            {
                "id": str(nid),
                "node_type": "composite",
                "position": {"x": 0, "y": 0},
                # composite_ref intentionally omitted
            }
        ]
        resp = _graph_response(nodes, [])
        # The node should still be present (passthrough)
        assert len(resp.nodes) == 1
        # A validation_issue must be present
        issues = [i for i in resp.validation_issues if i.node_id == str(nid)]
        assert issues
        assert issues[0].code == "node_legacy_data"
        assert issues[0].severity == "warning"

    def test_agent_without_agent_id_returns_with_warning(self) -> None:
        """An agent node missing agent_id fails _validate_agent_node but the
        read path must return it with a warning."""
        nid = uuid.uuid4()
        nodes = [
            {
                "id": str(nid),
                "node_type": "agent",
                "position": {"x": 0, "y": 0},
                "agent_id": None,
            }
        ]
        resp = _graph_response(nodes, [])
        assert len(resp.nodes) == 1
        issues = [i for i in resp.validation_issues if i.node_id == str(nid)]
        assert issues
        assert issues[0].code == "node_legacy_data"

    def test_sandbox_without_template_id_returns_with_warning(self) -> None:
        """A sandbox_agent node missing template_id fails
        _validate_sandbox_agent_node but the read path must return it."""
        nid = uuid.uuid4()
        nodes = [
            {
                "id": str(nid),
                "node_type": "sandbox_agent",
                "position": {"x": 0, "y": 0},
                "agent_prompt": "do something",
                # template_id intentionally omitted
            }
        ]
        resp = _graph_response(nodes, [])
        assert len(resp.nodes) == 1
        issues = [i for i in resp.validation_issues if i.node_id == str(nid)]
        assert issues

    def test_valid_edges_pass(self) -> None:
        src = uuid.uuid4()
        tgt = uuid.uuid4()
        edges = [_minimal_edge(src, tgt)]
        resp = _graph_response([], edges)
        assert len(resp.edges) == 1
        assert not resp.validation_issues

    def test_invalid_edge_type_returns_with_warning(self) -> None:
        """An edge with an edge_type not matching the pattern fails the
        regex validator but the read path must return it."""
        src = uuid.uuid4()
        tgt = uuid.uuid4()
        edge = _minimal_edge(src, tgt)
        edge["edge_type"] = "invalid_type"
        resp = _graph_response([], [edge])
        assert len(resp.edges) == 1
        assert resp.validation_issues
        assert resp.validation_issues[0].code == "edge_validation_failed"

    def test_mixed_valid_and_invalid_nodes(self) -> None:
        """A graph with some valid and some invalid nodes returns all of them,
        with issues only for the invalid ones."""
        valid_id = uuid.uuid4()
        invalid_id = uuid.uuid4()
        nodes = [
            _minimal_node(valid_id, "agent"),
            {
                "id": str(invalid_id),
                "node_type": "composite",
                "position": {"x": 0, "y": 0},
            },
        ]
        resp = _graph_response(nodes, [])
        assert len(resp.nodes) == 2
        invalid_issues = [i for i in resp.validation_issues if i.node_id == str(invalid_id)]
        assert invalid_issues

    def test_empty_graph(self) -> None:
        resp = _graph_response([], [])
        assert not resp.nodes
        assert not resp.edges
        assert not resp.validation_issues

    def test_preserves_existing_validation_issues(self) -> None:
        """Caller-supplied validation_issues are merged with any new ones."""
        existing = [GraphValidationIssue(severity="info", code="pre_existing", message="test")]
        resp = _graph_response([], [], validation_issues=existing)
        assert len(resp.validation_issues) == 1
        assert resp.validation_issues[0].code == "pre_existing"

    def test_raw_dict_passthrough_preserves_id(self) -> None:
        """Even when a node fails validation, its ID is preserved in the
        fallback passthrough."""
        nid = uuid.uuid4()
        nodes = [
            {
                "id": str(nid),
                "node_type": "composite",
                "position": {"x": 10, "y": 20},
            }
        ]
        resp = _graph_response(nodes, [])
        assert len(resp.nodes) == 1
        assert resp.nodes[0].id == nid


# ---------------------------------------------------------------------------
# Regression: every node_type shape tolerates reads
# ---------------------------------------------------------------------------


class TestGraphNodeTypesTolerantRead:
    """Every valid node_type shape passes reads without issues."""

    @pytest.mark.parametrize(
        "node_type",
        [
            "agent",
            "manual",
            "composite",
            "sandbox_agent",
            "router",
            "hitl",
            "join",
        ],
    )
    def test_valid_node_type_round_trips(self, node_type: str) -> None:
        nid = uuid.uuid4()
        node = _minimal_node(nid, node_type)
        resp = _graph_response([node], [])
        assert len(resp.nodes) == 1
        assert resp.nodes[0].node_type == node_type
        assert not resp.validation_issues


class TestGraphEdgeTypesTolerantRead:
    """Every valid edge_type shape passes reads without issues."""

    @pytest.mark.parametrize("edge_type", ["normal", "reject", "conditional", "loop"])
    def test_valid_edge_type_round_trips(self, edge_type: str) -> None:
        src, tgt = uuid.uuid4(), uuid.uuid4()
        edge = _minimal_edge(src, tgt, edge_type)
        resp = _graph_response([], [edge])
        assert len(resp.edges) == 1
        assert not resp.validation_issues


# ---------------------------------------------------------------------------
# Endpoint integration tests
# ---------------------------------------------------------------------------


def _make_mock_pipeline(nodes: list[dict[str, Any]], edges: list[dict[str, Any]] | None = None) -> MagicMock:
    """Create a mock pipeline with graph_nodes_json."""
    pipeline = MagicMock()
    pipeline.id = _PIPELINE_ID
    pipeline.graph_nodes_json = nodes
    pipeline.owner_team_id = None
    pipeline.deleted_at = None
    pipeline.visibility = "org"
    return pipeline


def _make_mock_edge(
    source_id: uuid.UUID,
    target_id: uuid.UUID,
    edge_type: str = "normal",
    hitl_gate_config: dict[str, Any] | None = None,
) -> MagicMock:
    edge = MagicMock()
    edge.source_node_id = source_id
    edge.target_node_id = target_id
    edge.edge_type = edge_type
    edge.hitl_gate_config = hitl_gate_config
    edge.condition_expression = None
    edge.source_port = "out"
    edge.target_port = "in"
    edge.id = uuid.uuid4()
    return edge


@pytest.fixture
def client() -> TestClient:
    mock_session = configure_mock_session(AsyncMock())
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    mock_session.begin = MagicMock(return_value=begin_cm)

    async def override_session() -> AsyncMock:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="testuser",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


class TestGetGraphEndpointNever422:
    """The GET /graph endpoint must never return 422."""

    def test_valid_graph_returns_200(self, client: TestClient) -> None:
        nid = uuid.uuid4()
        nodes = [_minimal_node(nid, "sandbox_agent")]
        pipeline = _make_mock_pipeline(nodes)

        with (
            patch("modulo.api.routes.pipelines.get_pipeline_graph", new_callable=AsyncMock, return_value=(nodes, [])),
            patch("modulo.api.routes.pipelines.get_pipeline", new_callable=AsyncMock, return_value=pipeline),
            patch("modulo.api.routes.pipelines.set_rls_org"),
            patch("modulo.api.routes.pipelines.set_rls_user_context"),
        ):
            resp = client.get(f"/api/v1/pipelines/{_PIPELINE_ID}/graph")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["nodes"]) == 1
        assert body["nodes"][0]["node_type"] == "sandbox_agent"

    def test_invalid_composite_node_returns_200_not_422(self, client: TestClient) -> None:
        """A stored composite node missing composite_ref must not 422."""
        nid = uuid.uuid4()
        nodes = [
            {
                "id": str(nid),
                "node_type": "composite",
                "position": {"x": 0, "y": 0},
            }
        ]
        pipeline = _make_mock_pipeline(nodes)

        with (
            patch("modulo.api.routes.pipelines.get_pipeline_graph", new_callable=AsyncMock, return_value=(nodes, [])),
            patch("modulo.api.routes.pipelines.get_pipeline", new_callable=AsyncMock, return_value=pipeline),
            patch("modulo.api.routes.pipelines.set_rls_org"),
            patch("modulo.api.routes.pipelines.set_rls_user_context"),
        ):
            resp = client.get(f"/api/v1/pipelines/{_PIPELINE_ID}/graph")
        # MUST be 200, not 422
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["nodes"]) == 1
        # Validation issue must be reported
        assert body["validation_issues"]
        assert body["validation_issues"][0]["code"] == "node_legacy_data"

    def test_invalid_agent_node_returns_200_not_422(self, client: TestClient) -> None:
        """An agent node without agent_id must not 422."""
        nid = uuid.uuid4()
        nodes = [
            {
                "id": str(nid),
                "node_type": "agent",
                "position": {"x": 0, "y": 0},
                "agent_id": None,
            }
        ]
        pipeline = _make_mock_pipeline(nodes)

        with (
            patch("modulo.api.routes.pipelines.get_pipeline_graph", new_callable=AsyncMock, return_value=(nodes, [])),
            patch("modulo.api.routes.pipelines.get_pipeline", new_callable=AsyncMock, return_value=pipeline),
            patch("modulo.api.routes.pipelines.set_rls_org"),
            patch("modulo.api.routes.pipelines.set_rls_user_context"),
        ):
            resp = client.get(f"/api/v1/pipelines/{_PIPELINE_ID}/graph")
        assert resp.status_code == 200
        body = resp.json()
        assert body["validation_issues"]

    def test_collection_pipeline_shape_returns_200(self, client: TestClient) -> None:
        """A 'Collection' pipeline with composite + agent + join nodes
        (mimicking Collection: GitHub PR Reviewer) must return 200."""
        composite_id = uuid.uuid4()
        agent_id = uuid.uuid4()
        join_id = uuid.uuid4()
        nodes = [
            _minimal_node(composite_id, "composite"),
            _minimal_node(agent_id, "agent"),
            _minimal_node(join_id, "join"),
        ]
        edges = [
            _minimal_edge(composite_id, agent_id),
            _minimal_edge(agent_id, join_id),
        ]
        pipeline = _make_mock_pipeline(nodes, edges)

        with (
            patch(
                "modulo.api.routes.pipelines.get_pipeline_graph",
                new_callable=AsyncMock,
                return_value=(nodes, edges),
            ),
            patch("modulo.api.routes.pipelines.get_pipeline", new_callable=AsyncMock, return_value=pipeline),
            patch("modulo.api.routes.pipelines.set_rls_org"),
            patch("modulo.api.routes.pipelines.set_rls_user_context"),
        ):
            resp = client.get(f"/api/v1/pipelines/{_PIPELINE_ID}/graph")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["nodes"]) == 3
        assert len(body["edges"]) == 2

    def test_graph_with_extra_unknown_fields_returns_200(self, client: TestClient) -> None:
        """Nodes with unknown extra fields (from a future schema addition) must
        not 422 — Pydantic ignores extra fields by default."""
        nid = uuid.uuid4()
        node = _minimal_node(nid, "sandbox_agent")
        node["future_field_xyz"] = "some_value"
        pipeline = _make_mock_pipeline([node])

        with (
            patch("modulo.api.routes.pipelines.get_pipeline_graph", new_callable=AsyncMock, return_value=([node], [])),
            patch("modulo.api.routes.pipelines.get_pipeline", new_callable=AsyncMock, return_value=pipeline),
            patch("modulo.api.routes.pipelines.set_rls_org"),
            patch("modulo.api.routes.pipelines.set_rls_user_context"),
        ):
            resp = client.get(f"/api/v1/pipelines/{_PIPELINE_ID}/graph")
        assert resp.status_code == 200
