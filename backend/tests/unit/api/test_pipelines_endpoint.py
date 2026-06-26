"""Unit tests for /api/v1/pipelines endpoints."""

import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session
from modulo.api.main import app
from modulo.api.routes.pipelines import PipelineGraphNode, _resolve_graph_references
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_PIPELINE_ID = uuid.uuid4()
_NOW = datetime(2025, 1, 1, tzinfo=UTC)


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


def _make_pipeline() -> MagicMock:
    p = MagicMock()
    p.id = _PIPELINE_ID
    p.organisation_id = _ORG_ID
    p.name = "Test Pipeline"
    p.description = None
    p.visibility = "org"
    p.max_concurrent_runs = 5
    p.lock_wait_timeout_seconds = 300
    p.node_timeout_seconds = 300
    p.run_context_defaults = {}
    p.default_autonomy_level = "manual_approval"
    p.created_by = uuid.uuid4()
    p.created_at = _NOW
    p.updated_at = _NOW
    return p


def _make_mock_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="testuser",
        organisation_id=_ORG_ID,
        user_id=_USER_ID,
        org_role="admin",
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture()
def unauth_client() -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_settings] = _make_settings
    yield TestClient(app)
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# GET /api/v1/pipelines
# ---------------------------------------------------------------------------


def test_list_pipelines_returns_200(client: TestClient) -> None:
    pipeline = _make_pipeline()
    page_result = MagicMock()
    page_result.items = [pipeline]
    page_result.total = 1
    page_result.page = 1
    page_result.page_size = 20

    with (
        patch("modulo.api.routes.pipelines.list_pipelines", return_value=page_result),
        patch("modulo.api.routes.pipelines.set_rls_org"),
            patch("modulo.api.routes.pipelines.set_rls_user_context"),
        ):
        resp = client.get("/api/v1/pipelines")

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["name"] == "Test Pipeline"


# ---------------------------------------------------------------------------
# POST /api/v1/pipelines
# ---------------------------------------------------------------------------


def test_create_pipeline_returns_201(client: TestClient) -> None:
    pipeline = _make_pipeline()

    with (
        patch("modulo.api.routes.pipelines.create_pipeline", return_value=pipeline) as create,
        patch("modulo.api.routes.pipelines.set_rls_org") as set_org,
            patch("modulo.api.routes.pipelines.set_rls_user_context") as set_user_ctx,
    ):
        resp = client.post("/api/v1/pipelines", json={"name": "Test Pipeline"})

    assert resp.status_code == 201
    assert resp.json()["name"] == "Test Pipeline"
    set_org.assert_awaited_once_with(ANY, _ORG_ID)
    set_user_ctx.assert_awaited_once_with(ANY, _USER_ID, "admin")
    assert create.await_args.kwargs["org_id"] == _ORG_ID
    assert create.await_args.kwargs["created_by"] == _USER_ID


def test_create_pipeline_default_autonomy_level(client: TestClient) -> None:
    pipeline = _make_pipeline()
    pipeline.default_autonomy_level = "notify_on_complete"

    with (
        patch("modulo.api.routes.pipelines.create_pipeline", return_value=pipeline) as create,
        patch("modulo.api.routes.pipelines.set_rls_org"),
            patch("modulo.api.routes.pipelines.set_rls_user_context"),
        ):
        resp = client.post(
            "/api/v1/pipelines",
            json={"name": "Pipeline", "default_autonomy_level": "notify_on_complete"},
        )

    assert resp.status_code == 201
    assert resp.json()["default_autonomy_level"] == "notify_on_complete"
    assert create.await_args.kwargs["default_autonomy_level"] == "notify_on_complete"


def test_create_pipeline_default_autonomy_default_value(client: TestClient) -> None:
    pipeline = _make_pipeline()
    pipeline.default_autonomy_level = "manual_approval"

    with (
        patch("modulo.api.routes.pipelines.create_pipeline", return_value=pipeline) as create,
        patch("modulo.api.routes.pipelines.set_rls_org"),
            patch("modulo.api.routes.pipelines.set_rls_user_context"),
        ):
        resp = client.post("/api/v1/pipelines", json={"name": "Pipeline"})

    assert resp.status_code == 201
    assert create.await_args.kwargs["default_autonomy_level"] == "manual_approval"


# ---------------------------------------------------------------------------
# GET /api/v1/pipelines/{id}
# ---------------------------------------------------------------------------


def test_get_pipeline_returns_200(client: TestClient) -> None:
    pipeline = _make_pipeline()

    with (
        patch("modulo.api.routes.pipelines.get_pipeline", return_value=pipeline),
        patch("modulo.api.routes.pipelines.set_rls_org"),
            patch("modulo.api.routes.pipelines.set_rls_user_context"),
        ):
        resp = client.get(f"/api/v1/pipelines/{_PIPELINE_ID}")

    assert resp.status_code == 200
    assert resp.json()["id"] == str(_PIPELINE_ID)


def test_get_pipeline_not_found_returns_404(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.pipelines.get_pipeline", return_value=None),
        patch("modulo.api.routes.pipelines.set_rls_org"),
            patch("modulo.api.routes.pipelines.set_rls_user_context"),
        ):
        resp = client.get(f"/api/v1/pipelines/{uuid.uuid4()}")

    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET/PATCH /api/v1/pipelines/{id}/graph
# ---------------------------------------------------------------------------


def test_get_pipeline_graph_returns_authoritative_graph(client: TestClient) -> None:
    node_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    edge = MagicMock()
    edge.id = uuid.uuid4()
    edge.source_node_id = node_id
    edge.target_node_id = uuid.uuid4()
    edge.edge_type = "normal"
    edge.condition_expression = None
    edge.hitl_gate_config = None
    nodes = [
        {
            "id": str(node_id),
            "agent_id": str(agent_id),
            "position": {"x": 10, "y": 20},
            "connector_binding": None,
        }
    ]

    with (
        patch("modulo.api.routes.pipelines.get_pipeline_graph", return_value=(nodes, [edge])),
        patch("modulo.api.routes.pipelines.set_rls_org"),
            patch("modulo.api.routes.pipelines.set_rls_user_context"),
        ):
        resp = client.get(f"/api/v1/pipelines/{_PIPELINE_ID}/graph")

    assert resp.status_code == 200
    assert resp.json()["nodes"][0]["agent_id"] == str(agent_id)
    assert resp.json()["edges"][0]["id"] == str(edge.id)


def test_replace_pipeline_graph_returns_soft_validation_issues(client: TestClient) -> None:
    node_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    nodes = [
        {
            "id": str(node_id),
            "agent_id": str(agent_id),
            "position": {"x": 10, "y": 20},
            "connector_binding": None,
        }
    ]
    validation = MagicMock()
    validation.issues = [
        MagicMock(
            severity="warning",
            code="TOPOLOGY_UNREACHABLE",
            message="draft warning",
            node_id=str(node_id),
        )
    ]
    schema_pins = [{"node_id": str(node_id), "direction": "output", "schema_id": str(uuid.uuid4())}]
    backend_pins = [{"node_id": str(node_id), "model_backend_id": str(uuid.uuid4())}]

    with (
        patch(
            "modulo.api.routes.pipelines.replace_pipeline_graph",
            return_value=(nodes, []),
        ),
        patch(
            "modulo.api.routes.pipelines.GraphValidator.validate_definition",
            return_value=validation,
        ) as validate,
        patch(
            "modulo.api.routes.pipelines._resolve_graph_references",
            return_value=(schema_pins, backend_pins),
        ),
        patch("modulo.api.routes.pipelines.set_rls_org"),
            patch("modulo.api.routes.pipelines.set_rls_user_context"),
        ):
        resp = client.patch(
            f"/api/v1/pipelines/{_PIPELINE_ID}/graph",
            json={"nodes": nodes, "edges": []},
        )

    assert resp.status_code == 200
    assert resp.json()["validation_issues"][0]["code"] == "TOPOLOGY_UNREACHABLE"
    validate.assert_awaited_once()
    assert validate.await_args.kwargs["schema_pins"] == schema_pins
    assert validate.await_args.kwargs["model_backend_pins"] == backend_pins


def test_replace_pipeline_graph_rejects_duplicate_paths(client: TestClient) -> None:
    source = uuid.uuid4()
    target = uuid.uuid4()
    edge = {
        "source_node_id": str(source),
        "target_node_id": str(target),
        "edge_type": "normal",
        "hitl_gate_config": None,
    }
    resp = client.patch(
        f"/api/v1/pipelines/{_PIPELINE_ID}/graph",
        json={"nodes": [], "edges": [edge, edge]},
    )

    assert resp.status_code == 422


def test_replace_pipeline_graph_accepts_manual_node_contract(client: TestClient) -> None:
    node_id = uuid.uuid4()
    output_schema_id = uuid.uuid4()
    nodes = [
        {
            "id": str(node_id),
            "node_type": "manual",
            "agent_id": None,
            "position": {"x": 10, "y": 20},
            "connector_binding": None,
            "output_schema_id": str(output_schema_id),
            "label": "QA sign-off",
            "role": None,
            "autonomy_recommendation": None,
        }
    ]
    validation = MagicMock(issues=[])

    with (
        patch(
            "modulo.api.routes.pipelines.replace_pipeline_graph",
            return_value=(nodes, []),
        ),
        patch(
            "modulo.api.routes.pipelines.GraphValidator.validate_definition",
            return_value=validation,
        ),
        patch(
            "modulo.api.routes.pipelines._resolve_graph_references",
            return_value=([], []),
        ),
        patch("modulo.api.routes.pipelines.set_rls_org"),
            patch("modulo.api.routes.pipelines.set_rls_user_context"),
        ):
        resp = client.patch(
            f"/api/v1/pipelines/{_PIPELINE_ID}/graph",
            json={"nodes": nodes, "edges": []},
        )

    assert resp.status_code == 200
    assert resp.json()["nodes"][0] == nodes[0]


def test_replace_pipeline_graph_rejects_excessive_node_count(client: TestClient) -> None:
    nodes = [
        {"id": str(uuid.uuid4()), "agent_id": str(uuid.uuid4()), "position": {"x": i * 10, "y": 0}}
        for i in range(501)
    ]
    resp = client.patch(
        f"/api/v1/pipelines/{_PIPELINE_ID}/graph",
        json={"nodes": nodes, "edges": []},
    )
    assert resp.status_code == 422
    assert "exceeds maximum" in resp.json()["error"]["detail"]


def test_replace_pipeline_graph_rejects_excessive_edge_count(client: TestClient) -> None:
    node_a = uuid.uuid4()
    node_b = uuid.uuid4()
    edges = [
        {"source_node_id": str(node_a), "target_node_id": str(node_b), "edge_type": "normal"}
        for _ in range(1001)
    ]
    resp = client.patch(
        f"/api/v1/pipelines/{_PIPELINE_ID}/graph",
        json={
            "nodes": [
                {"id": str(node_a), "agent_id": str(uuid.uuid4()), "position": {"x": 0, "y": 0}},
                {"id": str(node_b), "agent_id": str(uuid.uuid4()), "position": {"x": 10, "y": 0}},
            ],
            "edges": edges,
        },
    )
    assert resp.status_code == 422
    assert "exceeds maximum" in resp.json()["error"]["detail"]


@pytest.mark.parametrize(
    "node",
    [
        {
            "node_type": "manual",
            "agent_id": str(uuid.uuid4()),
            "output_schema_id": str(uuid.uuid4()),
            "label": "Invalid manual node",
        },
        {
            "node_type": "agent",
            "agent_id": None,
        },
    ],
)
def test_replace_pipeline_graph_rejects_node_type_conflicts(
    client: TestClient, node: dict[str, object]
) -> None:
    body = {
        "id": str(uuid.uuid4()),
        "position": {"x": 10, "y": 20},
        "connector_binding": None,
        **node,
    }

    resp = client.patch(
        f"/api/v1/pipelines/{_PIPELINE_ID}/graph",
        json={"nodes": [body], "edges": []},
    )

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_graph_references_resolve_tenant_owned_agents_and_schemas() -> None:
    agent_id = uuid.uuid4()
    manual_schema_id = uuid.uuid4()
    agent_node_id = uuid.uuid4()
    manual_node_id = uuid.uuid4()
    agent = MagicMock(
        id=agent_id,
        input_schema_id=uuid.uuid4(),
        output_schema_id=uuid.uuid4(),
        model_backend_id=uuid.uuid4(),
    )
    agent_result = MagicMock()
    agent_result.scalars.return_value = [agent]
    schema_result = MagicMock()
    schema_result.scalars.return_value = [manual_schema_id]
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[agent_result, schema_result])
    nodes = [
        PipelineGraphNode.model_validate(
            {
                "id": agent_node_id,
                "node_type": "agent",
                "agent_id": agent_id,
                "position": {"x": 0, "y": 0},
            }
        ),
        PipelineGraphNode.model_validate(
            {
                "id": manual_node_id,
                "node_type": "manual",
                "agent_id": None,
                "position": {"x": 1, "y": 1},
                "output_schema_id": manual_schema_id,
                "label": "Approval",
            }
        ),
    ]

    schema_pins, backend_pins = await _resolve_graph_references(session, nodes, _ORG_ID)

    assert schema_pins == [
        {
            "node_id": str(agent_node_id),
            "direction": "input",
            "schema_id": str(agent.input_schema_id),
        },
        {
            "node_id": str(agent_node_id),
            "direction": "output",
            "schema_id": str(agent.output_schema_id),
        },
        {
            "node_id": str(manual_node_id),
            "direction": "output",
            "schema_id": str(manual_schema_id),
        },
    ]
    assert backend_pins == [
        {
            "node_id": str(agent_node_id),
            "model_backend_id": str(agent.model_backend_id),
        }
    ]


@pytest.mark.asyncio
async def test_graph_references_reject_unknown_agent() -> None:
    result = MagicMock()
    result.scalars.return_value = []
    session = AsyncMock()
    session.execute = AsyncMock(return_value=result)
    node = PipelineGraphNode.model_validate(
        {
            "id": uuid.uuid4(),
            "node_type": "agent",
            "agent_id": uuid.uuid4(),
            "position": {"x": 0, "y": 0},
        }
    )

    with pytest.raises(HTTPException) as exc_info:
        await _resolve_graph_references(session, [node], _ORG_ID)

    assert exc_info.value.status_code == 422
    assert "Unknown agent IDs" in exc_info.value.detail


@pytest.mark.asyncio
async def test_graph_references_reject_unknown_manual_schema() -> None:
    result = MagicMock()
    result.scalars.return_value = []
    session = AsyncMock()
    session.execute = AsyncMock(return_value=result)
    node = PipelineGraphNode.model_validate(
        {
            "id": uuid.uuid4(),
            "node_type": "manual",
            "agent_id": None,
            "position": {"x": 0, "y": 0},
            "output_schema_id": uuid.uuid4(),
            "label": "Approval",
        }
    )

    with pytest.raises(HTTPException) as exc_info:
        await _resolve_graph_references(session, [node], _ORG_ID)

    assert exc_info.value.status_code == 422
    assert "Unknown manual output schema IDs" in exc_info.value.detail


# ---------------------------------------------------------------------------
# PATCH /api/v1/pipelines/{id}
# ---------------------------------------------------------------------------


def test_update_pipeline_returns_200(client: TestClient) -> None:
    pipeline = _make_pipeline()
    pipeline.name = "Updated"

    with (
        patch("modulo.api.routes.pipelines.update_pipeline", return_value=pipeline),
        patch("modulo.api.routes.pipelines.set_rls_org"),
            patch("modulo.api.routes.pipelines.set_rls_user_context"),
        ):
        resp = client.patch(f"/api/v1/pipelines/{_PIPELINE_ID}", json={"name": "Updated"})

    assert resp.status_code == 200
    assert resp.json()["name"] == "Updated"


def test_update_pipeline_autonomy_level(client: TestClient) -> None:
    pipeline = _make_pipeline()
    pipeline.default_autonomy_level = "manual_approval"
    updated = _make_pipeline()
    updated.default_autonomy_level = "fully_autonomous"

    with (
        patch("modulo.api.routes.pipelines.update_pipeline", return_value=updated),
        patch("modulo.api.routes.pipelines.get_pipeline", new=AsyncMock(return_value=pipeline)),
        patch("modulo.api.routes.pipelines.set_rls_org"),
        patch("modulo.api.routes.pipelines.append_audit_event") as mock_audit,
    ):
        resp = client.patch(
            f"/api/v1/pipelines/{_PIPELINE_ID}",
            json={"default_autonomy_level": "fully_autonomous"},
        )

    assert resp.status_code == 200
    assert resp.json()["default_autonomy_level"] == "fully_autonomous"
    mock_audit.assert_awaited_once()


def test_update_pipeline_autonomy_level_unchanged_no_audit(client: TestClient) -> None:
    pipeline = _make_pipeline()
    pipeline.default_autonomy_level = "manual_approval"

    with (
        patch("modulo.api.routes.pipelines.update_pipeline", return_value=pipeline),
        patch("modulo.api.routes.pipelines.get_pipeline", new=AsyncMock(return_value=pipeline)),
        patch("modulo.api.routes.pipelines.set_rls_org"),
        patch("modulo.api.routes.pipelines.append_audit_event") as mock_audit,
    ):
        resp = client.patch(
            f"/api/v1/pipelines/{_PIPELINE_ID}",
            json={"name": "just a rename"},
        )

    assert resp.status_code == 200
    mock_audit.assert_not_awaited()


def test_update_pipeline_not_found_returns_404(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.pipelines.update_pipeline", return_value=None),
        patch("modulo.api.routes.pipelines.set_rls_org"),
            patch("modulo.api.routes.pipelines.set_rls_user_context"),
        ):
        resp = client.patch(f"/api/v1/pipelines/{uuid.uuid4()}", json={"name": "x"})

    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# DELETE /api/v1/pipelines/{id}
# ---------------------------------------------------------------------------


def test_delete_pipeline_returns_204(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.pipelines.delete_pipeline", return_value=True),
        patch("modulo.api.routes.pipelines.set_rls_org"),
            patch("modulo.api.routes.pipelines.set_rls_user_context"),
        ):
        resp = client.delete(f"/api/v1/pipelines/{_PIPELINE_ID}")

    assert resp.status_code == 204


def test_delete_pipeline_not_found_returns_404(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.pipelines.delete_pipeline", return_value=False),
        patch("modulo.api.routes.pipelines.set_rls_org"),
            patch("modulo.api.routes.pipelines.set_rls_user_context"),
        ):
        resp = client.delete(f"/api/v1/pipelines/{uuid.uuid4()}")

    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/v1/pipelines/{id}/clone
# ---------------------------------------------------------------------------


def test_clone_pipeline_returns_201(client: TestClient) -> None:
    cloned = _make_pipeline()
    cloned.name = "Copy of Test Pipeline"
    cloned.id = uuid.uuid4()

    with (
        patch("modulo.api.routes.pipelines.clone_pipeline", return_value=cloned) as mock_clone,
        patch("modulo.api.routes.pipelines.set_rls_org"),
            patch("modulo.api.routes.pipelines.set_rls_user_context"),
        ):
        resp = client.post(f"/api/v1/pipelines/{_PIPELINE_ID}/clone", json={})

    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "Copy of Test Pipeline"
    assert body["id"] != str(_PIPELINE_ID)
    mock_clone.assert_awaited_once_with(
        ANY,
        org_id=_ORG_ID,
        pipeline_id=_PIPELINE_ID,
        created_by=_USER_ID,
        new_name=None,
    )


def test_clone_pipeline_with_custom_name(client: TestClient) -> None:
    cloned = _make_pipeline()
    cloned.name = "My Custom Clone"
    cloned.id = uuid.uuid4()

    with (
        patch("modulo.api.routes.pipelines.clone_pipeline", return_value=cloned) as mock_clone,
        patch("modulo.api.routes.pipelines.set_rls_org"),
            patch("modulo.api.routes.pipelines.set_rls_user_context"),
        ):
        resp = client.post(
            f"/api/v1/pipelines/{_PIPELINE_ID}/clone",
            json={"name": "My Custom Clone"},
        )

    assert resp.status_code == 201
    assert resp.json()["name"] == "My Custom Clone"
    mock_clone.assert_awaited_once_with(
        ANY,
        org_id=_ORG_ID,
        pipeline_id=_PIPELINE_ID,
        created_by=_USER_ID,
        new_name="My Custom Clone",
    )


def test_clone_pipeline_not_found_returns_404(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.pipelines.clone_pipeline", return_value=None),
        patch("modulo.api.routes.pipelines.set_rls_org"),
            patch("modulo.api.routes.pipelines.set_rls_user_context"),
        ):
        resp = client.post(f"/api/v1/pipelines/{uuid.uuid4()}/clone", json={})

    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Unauthenticated
# ---------------------------------------------------------------------------


def test_list_pipelines_unauthenticated_returns_4xx(unauth_client: TestClient) -> None:
    resp = unauth_client.get("/api/v1/pipelines")
    assert resp.status_code in (401, 403)
