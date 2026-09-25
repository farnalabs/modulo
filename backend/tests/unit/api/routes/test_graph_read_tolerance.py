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
from modulo.api.middleware.sensitive_mask import SENSITIVE_VALUE_MASK
from modulo.api.routes.pipelines import (
    GraphValidationIssue,
    _graph_response,
)
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.db.models.pipeline_edge import PipelineEdge
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


def _orm_edge(**extra: Any) -> PipelineEdge:
    """Build a real ORM ``PipelineEdge`` row — the shape ``get_pipeline_graph`` returns."""
    edge = PipelineEdge()
    edge.id = uuid.uuid4()
    edge.source_node_id = uuid.uuid4()
    edge.target_node_id = uuid.uuid4()
    edge.edge_type = "normal"
    edge.hitl_gate_config = None
    edge.condition_expression = None
    edge.source_port = "out"
    edge.target_port = "in"
    edge.retry = None
    edge.on_failure_target = None
    for key, value in extra.items():
        setattr(edge, key, value)
    return edge


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


# ---------------------------------------------------------------------------
# FAR-874 hardening: data-loss, secret-leak, edge-fallback, tier-3, non-dict
# ---------------------------------------------------------------------------


class TestDataLossRegression:
    """Tier-3 fallback must preserve all stored fields, not truncate to id/type/position."""

    def test_tier3_node_preserves_distinctive_fields(self) -> None:
        """A node that fails BOTH strict and lenient validation must still
        carry every stored field the model declares (label, template_id, etc.)
        in the returned object."""
        nid = uuid.uuid4()
        # agent_id="not-a-uuid" fails Pydantic UUID coercion in BOTH strict
        # and lenient mode (legacy_read only skips model-level validators, not
        # field-level type coercion) — so this hits the tier-3 fallback.
        nodes = [
            {
                "id": str(nid),
                "node_type": "agent",
                "position": {"x": 42, "y": 7},
                "agent_id": "not-a-uuid",
                "label": "DISTINCTIVE_LABEL_SENTINEL",
                "template_id": "DISTINCTIVE_TEMPLATE_SENTINEL",
                "agent_prompt": "do something important",
                "env_vars": {"MY_VAR": "keep_me"},
            }
        ]
        resp = _graph_response(nodes, [])
        assert len(resp.nodes) == 1
        node = resp.nodes[0]
        # The fallback must preserve stored fields
        assert str(node.id) == str(nid)
        assert node.node_type == "agent"
        assert node.label == "DISTINCTIVE_LABEL_SENTINEL"
        assert node.template_id == "DISTINCTIVE_TEMPLATE_SENTINEL"
        assert node.agent_prompt == "do something important"
        assert node.env_vars == {"MY_VAR": "keep_me"}
        # Must report node_validation_failed (tier-3), not node_legacy_data (tier-2)
        issues = [i for i in resp.validation_issues if i.node_id == str(nid)]
        assert len(issues) == 1
        assert issues[0].code == "node_validation_failed"

    def test_tier3_endpoint_preserves_fields(self, client: TestClient) -> None:
        """Same as above but through the GET /graph endpoint."""
        nid = uuid.uuid4()
        nodes = [
            {
                "id": str(nid),
                "node_type": "agent",
                "position": {"x": 1, "y": 2},
                "agent_id": "not-a-uuid",
                "label": "ENDPOINT_LABEL",
                "template_id": "ENDPOINT_TEMPLATE",
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
        node = body["nodes"][0]
        assert node["label"] == "ENDPOINT_LABEL"
        assert node["template_id"] == "ENDPOINT_TEMPLATE"
        assert node["agent_id"] == "not-a-uuid"
        assert any(i["code"] == "node_validation_failed" for i in body["validation_issues"])


class TestSecretNonLeak:
    """validation_issues messages must never contain env_vars / secrets."""

    def test_env_vars_not_in_validation_issues(self) -> None:
        """A node whose env_vars contains a sentinel that fails validation
        must NOT have that sentinel appear in the validation_issues messages."""
        sentinel = "SK-1234567890ABCDEF_SECRET_KEY_LEAK"
        nid = uuid.uuid4()
        nodes = [
            {
                "id": str(nid),
                "node_type": "agent",
                "position": {"x": 0, "y": 0},
                "agent_id": "not-a-uuid",
                "env_vars": {"OPENAI_API_KEY": sentinel},
            }
        ]
        resp = _graph_response(nodes, [])
        # Non-vacuous: the invalid node produced at least one validation issue.
        assert resp.validation_issues
        # The sentinel must NOT appear in any validation issue message
        for issue in resp.validation_issues:
            assert sentinel not in issue.message
        # FAR-1181: the graph read masks credential-bearing env values, so the
        # sentinel is no longer returned raw on the node either.
        node = resp.nodes[0]
        assert node.env_vars == {"OPENAI_API_KEY": SENSITIVE_VALUE_MASK}

    def test_env_vars_not_in_endpoint_validation_issues(self, client: TestClient) -> None:
        """Same check through the GET /graph endpoint — the sentinel must not
        appear in validation_issues messages."""
        sentinel = "PK-LEAK_TEST_987654321"
        nid = uuid.uuid4()
        nodes = [
            {
                "id": str(nid),
                "node_type": "agent",
                "position": {"x": 0, "y": 0},
                "agent_id": "not-a-uuid",
                "env_vars": {"SECRET_TOKEN": sentinel},
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
        for issue in body["validation_issues"]:
            assert sentinel not in issue["message"]

    def test_connector_binding_not_in_issue_message(self) -> None:
        """connector_binding values must not leak into validation_issues messages."""
        nid = uuid.uuid4()
        secret_instance = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        nodes = [
            {
                "id": str(nid),
                "node_type": "agent",
                "position": {"x": 0, "y": 0},
                "agent_id": "not-a-uuid",
                "connector_binding": {"type": "github", "instance_id": secret_instance},
            }
        ]
        resp = _graph_response(nodes, [])
        for issue in resp.validation_issues:
            assert secret_instance not in issue.message


class TestEdgeFallbackPreservesConfig:
    """Edge fallback must preserve hitl_gate_config and condition_expression."""

    def test_edge_hitl_gate_config_preserved(self) -> None:
        """An edge with hitl_gate_config that fails validation must still
        return its gate config in the fallback."""
        src, tgt = uuid.uuid4(), uuid.uuid4()
        gate_config = {
            "label": "Human Review",
            "description": "Must review before merge - long enough description",
            "claim_expiry_minutes": 60,
        }
        edge = {
            "id": str(uuid.uuid4()),
            "source_node_id": str(src),
            "target_node_id": str(tgt),
            "edge_type": "INVALID_EDGE_TYPE",
            "hitl_gate_config": gate_config,
            "condition_expression": "state.approved == true",
        }
        resp = _graph_response([], [edge])
        assert len(resp.edges) == 1
        returned = resp.edges[0]
        # hitl_gate_config is preserved (raw dict from model_construct)
        assert returned.hitl_gate_config is not None
        assert returned.hitl_gate_config["label"] == "Human Review"
        assert returned.condition_expression == "state.approved == true"
        assert any(i.code == "edge_validation_failed" for i in resp.validation_issues)


class TestTier3Reachability:
    """Confirm a test actually reaches node_validation_failed (tier-3)."""

    def test_tier3_reached_on_invalid_uuid(self) -> None:
        """An agent node with agent_id='not-a-uuid' fails both strict and
        lenient validation, reaching tier-3 (node_validation_failed)."""
        nid = uuid.uuid4()
        nodes = [
            {
                "id": str(nid),
                "node_type": "agent",
                "position": {"x": 0, "y": 0},
                "agent_id": "not-a-uuid",
            }
        ]
        resp = _graph_response(nodes, [])
        issues = [i for i in resp.validation_issues if i.node_id == str(nid)]
        assert len(issues) == 1
        assert issues[0].code == "node_validation_failed"
        # Must NOT be tier-2 (node_legacy_data)
        assert issues[0].code != "node_legacy_data"

    def test_tier2_reached_on_missing_composite_ref(self) -> None:
        """Sanity: a composite without composite_ref reaches tier-2 (lenient
        passes), NOT tier-3."""
        nid = uuid.uuid4()
        nodes = [
            {
                "id": str(nid),
                "node_type": "composite",
                "position": {"x": 0, "y": 0},
            }
        ]
        resp = _graph_response(nodes, [])
        issues = [i for i in resp.validation_issues if i.node_id == str(nid)]
        assert len(issues) == 1
        assert issues[0].code == "node_legacy_data"


class TestNonDictNodeEntry:
    """A nodes list containing a non-dict must not raise."""

    def test_string_in_nodes_list(self) -> None:
        nodes: list[Any] = ["not-a-dict", 42, None]
        resp = _graph_response(nodes, [])  # type: ignore[arg-type]
        # Non-dict entries are skipped; no crash
        assert not resp.nodes

    def test_string_in_nodes_list_mixed_with_valid(self) -> None:
        nid = uuid.uuid4()
        nodes: list[Any] = ["garbage", _minimal_node(nid, "agent")]
        resp = _graph_response(nodes, [])  # type: ignore[arg-type]
        assert len(resp.nodes) == 1
        assert resp.nodes[0].id == nid


class TestNonDictEdgeEntry:
    """An edges list containing a non-dict must not raise.

    Symmetric with ``TestNonDictNodeEntry``: a malformed (non-dict) entry in
    the stored edges array previously reached ``edge_dict.get("id")`` and
    raised ``AttributeError`` -> HTTP 500, breaking the "graph read never
    fails" contract (FAR-874).
    """

    def test_string_in_edges_list(self) -> None:
        edges: list[Any] = ["not-a-dict", 42, None]
        resp = _graph_response([], edges)  # type: ignore[arg-type]
        # Non-dict entries are skipped; no crash
        assert not resp.edges

    def test_string_in_edges_list_mixed_with_valid(self) -> None:
        src, tgt = uuid.uuid4(), uuid.uuid4()
        edges: list[Any] = ["garbage", _minimal_edge(src, tgt)]
        resp = _graph_response([], edges)  # type: ignore[arg-type]
        assert len(resp.edges) == 1
        assert resp.edges[0].source_node_id == src
        assert resp.edges[0].target_node_id == tgt

    def test_non_dict_edge_entry_reports_issue(self) -> None:
        """The skipped non-dict entry is surfaced as a warning issue so the
        malformed stored data is not silently dropped."""
        edges: list[Any] = ["garbage"]
        resp = _graph_response([], edges)  # type: ignore[arg-type]
        assert not resp.edges
        issues = [i for i in resp.validation_issues if i.code == "edge_invalid_entry"]
        assert len(issues) == 1
        assert issues[0].severity == "warning"

    def test_string_in_edges_list_endpoint(self, client: TestClient) -> None:
        """Same as above but through the GET /graph endpoint: a non-dict edge
        entry must yield 200, not a 500."""
        src, tgt = uuid.uuid4(), uuid.uuid4()
        nodes = [_minimal_node(src, "agent"), _minimal_node(tgt, "agent")]
        edges: list[Any] = ["not-a-dict", _minimal_edge(src, tgt)]
        pipeline = _make_mock_pipeline(nodes)

        with (
            patch(
                "modulo.api.routes.pipelines.get_pipeline_graph", new_callable=AsyncMock, return_value=(nodes, edges)
            ),
            patch("modulo.api.routes.pipelines.get_pipeline", new_callable=AsyncMock, return_value=pipeline),
            patch("modulo.api.routes.pipelines.set_rls_org"),
            patch("modulo.api.routes.pipelines.set_rls_user_context"),
        ):
            resp = client.get(f"/api/v1/pipelines/{_PIPELINE_ID}/graph")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["edges"]) == 1
        assert any(i["code"] == "edge_invalid_entry" for i in body["validation_issues"])

    def test_orm_edge_object_is_not_skipped(self) -> None:
        """``get_pipeline_graph`` returns ORM rows, not dicts — a real edge must
        survive the read (regression guard for ``.get`` on an ORM row)."""
        edge = _orm_edge()
        resp = _graph_response([], [edge])
        assert len(resp.edges) == 1
        assert resp.edges[0].id == edge.id
        assert resp.edges[0].source_node_id == edge.source_node_id
        assert not any(i.code == "edge_invalid_entry" for i in resp.validation_issues)

    def test_orm_edge_failing_validation_is_preserved(self) -> None:
        """An ORM edge whose hitl_gate_config fails the current schema must be
        returned through the fallback (preserving stored fields), not dropped."""
        edge = _orm_edge(hitl_gate_config={"label": "x"})
        resp = _graph_response([], [edge])
        assert len(resp.edges) == 1
        assert resp.edges[0].hitl_gate_config == {"label": "x"}
        assert any(i.code == "edge_validation_failed" for i in resp.validation_issues)


class TestBrokenButRealisticGraph:
    """Composite node with composite_ref: None + an edge pointing at it."""

    def test_composite_none_ref_with_edge(self) -> None:
        """A composite with composite_ref=None (broken but stored) plus an
        edge pointing at it returns 200 with issues."""
        composite_id = uuid.uuid4()
        agent_id = uuid.uuid4()
        nodes = [
            {
                "id": str(composite_id),
                "node_type": "composite",
                "position": {"x": 0, "y": 0},
                "composite_ref": None,
            },
            _minimal_node(agent_id, "agent"),
        ]
        edges = [_minimal_edge(composite_id, agent_id)]
        resp = _graph_response(nodes, edges)
        assert len(resp.nodes) == 2
        assert len(resp.edges) == 1
        # Composite must have a validation issue
        composite_issues = [i for i in resp.validation_issues if i.node_id == str(composite_id)]
        assert composite_issues
        # Edge must be valid
        assert not any(i.code == "edge_validation_failed" for i in resp.validation_issues)
