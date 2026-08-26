"""Unit tests for /api/v1/pipelines endpoints."""

import asyncio
import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException, status
from fastapi.testclient import TestClient
from sqlalchemy.sql import Select

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.api.routes.pipelines import PipelineGraphNode, _resolve_graph_references
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.core.graph_validator._types import ValidationResult
from modulo.db.crud.hitl_gate_guard import GuardrailBindingStripDenied
from modulo.db.crud.pipeline_folder import create_folder, update_folder
from modulo.settings import Settings, get_settings
from tests.unit.api.mock_session import configure_mock_session

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
    p.rate_limit_config = None
    p.max_duration_seconds = None
    p.archived_at = None
    p.snapshot_count = 0
    p.id = _PIPELINE_ID
    p.organisation_id = _ORG_ID
    p.name = "Test Pipeline"
    p.description = None
    p.visibility = "org"
    p.owner_team_id = None
    p.folder_id = None
    p.max_concurrent_runs = 5
    p.lock_wait_timeout_seconds = 300
    p.node_timeout_seconds = 300
    p.run_context_defaults = {}
    p.default_autonomy_level = "manual_approval"
    p.rate_limit_config = None
    p.max_duration_seconds = None
    p.archived_at = None
    p.snapshot_count = 0
    p.created_by = uuid.uuid4()
    p.account_id = p.created_by
    p.created_at = _NOW
    p.updated_at = _NOW
    return p


def _make_mock_session() -> AsyncMock:
    session = configure_mock_session(AsyncMock())
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    session.begin_nested = MagicMock(return_value=begin_cm)
    return session


@pytest.fixture
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
        account_id=_USER_ID,
        org_role="admin",
    )
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def unauth_client() -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_settings] = _make_settings
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def operator_client() -> Generator[TestClient, None, None]:
    """Authenticated as a NON-ADMIN operator — used for the FAR-309 PR A
    mutation-time guardrail-strip enforcement (non-admins cannot strip a
    guardrail binding from a node)."""
    mock_session = _make_operator_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="operator@test",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="operator",
    )
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


def _make_operator_mock_session() -> AsyncMock:
    """Non-admin session: the team-scope dependency (require_team_membership_or_admin)
    runs a ``FROM pipelines`` SELECT for a non-admin caller — stub it to a
    org-visible pipeline (owner_team_id None → membership not required) so the
    graph-save route under test actually reaches the handler."""
    session = _make_mock_session()
    base_effect = session.execute.side_effect

    async def _execute(stmt: object, *args: Any, **kwargs: Any) -> Any:
        if isinstance(stmt, Select) and "FROM pipelines" in str(stmt):
            row = MagicMock()
            row.first.return_value = (None, "org")
            return row
        return base_effect(stmt, *args, **kwargs)

    session.execute = AsyncMock(side_effect=_execute)
    return session


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
    page_result.next_cursor = None
    page_result.has_more = False

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
    assert create.await_args.kwargs["account_id"] == _USER_ID


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


def test_create_pipeline_passes_stale_run_timeout_minutes(client: TestClient) -> None:
    pipeline = _make_pipeline()
    pipeline.stale_run_timeout_minutes = 45

    with (
        patch("modulo.api.routes.pipelines.create_pipeline", return_value=pipeline) as create,
        patch("modulo.api.routes.pipelines.set_rls_org"),
        patch("modulo.api.routes.pipelines.set_rls_user_context"),
    ):
        resp = client.post(
            "/api/v1/pipelines",
            json={"name": "Pipeline", "stale_run_timeout_minutes": 45},
        )

    assert resp.status_code == 201
    assert resp.json()["stale_run_timeout_minutes"] == 45
    assert create.await_args.kwargs["stale_run_timeout_minutes"] == 45


def test_create_pipeline_defaults_stale_run_timeout_minutes(client: TestClient) -> None:
    pipeline = _make_pipeline()
    pipeline.stale_run_timeout_minutes = 30

    with (
        patch("modulo.api.routes.pipelines.create_pipeline", return_value=pipeline) as create,
        patch("modulo.api.routes.pipelines.set_rls_org"),
        patch("modulo.api.routes.pipelines.set_rls_user_context"),
    ):
        resp = client.post("/api/v1/pipelines", json={"name": "Pipeline"})

    assert resp.status_code == 201
    assert resp.json()["stale_run_timeout_minutes"] == 30
    assert create.await_args.kwargs["stale_run_timeout_minutes"] == 30


def test_create_pipeline_rejects_null_stale_run_timeout(client: TestClient) -> None:
    resp = client.post("/api/v1/pipelines", json={"name": "Pipeline", "stale_run_timeout_minutes": None})

    assert resp.status_code == 422


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
    edge.source_port = "out"
    edge.target_port = "in"
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
        patch("modulo.api.routes.pipelines.get_pipeline", return_value=_make_pipeline()),
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
        patch("modulo.api.routes.pipelines.get_pipeline", return_value=_make_pipeline()),
        patch("modulo.api.routes.pipelines.set_rls_org"),
        patch("modulo.api.routes.pipelines.set_rls_user_context"),
    ):
        resp = client.patch(
            f"/api/v1/pipelines/{_PIPELINE_ID}/graph",
            json={"nodes": nodes, "edges": []},
        )

    assert resp.status_code == 200
    actual = resp.json()["nodes"][0]
    for key in (
        "id",
        "node_type",
        "agent_id",
        "position",
        "connector_binding",
        "output_schema_id",
        "label",
        "role",
        "autonomy_recommendation",
    ):
        assert actual.get(key) == nodes[0].get(key), (
            f"Mismatch on key '{key}': {actual.get(key)} != {nodes[0].get(key)}"
        )


def _sandbox_node_json() -> dict[str, object]:
    return {
        "id": str(uuid.uuid4()),
        "node_type": "sandbox_agent",
        "agent_id": None,
        "position": {"x": 10, "y": 20},
        "connector_binding": None,
        "agent_prompt": "Do the thing",
        "agent_command": "opencode run --auto < /home/user/prompt.md",
        "template_id": "opencode",
    }


def test_pipeline_graph_node_delivery_sentinel_round_trip() -> None:
    """FAR-228: PipelineGraphNode carries delivery_sentinel (opt-in idempotency
    gate marker) and tolerates its absence (legacy nodes default to None)."""
    node = PipelineGraphNode.model_validate({**_sandbox_node_json(), "delivery_sentinel": "EMAIL_SENT"})
    assert node.delivery_sentinel == "EMAIL_SENT"

    legacy = PipelineGraphNode.model_validate(_sandbox_node_json())
    assert legacy.delivery_sentinel is None


def test_pipeline_graph_node_idempotent_round_trip() -> None:
    """FAR-295: PipelineGraphNode carries ``idempotent`` on every executor type
    and defaults it to true for legacy nodes."""
    node = PipelineGraphNode.model_validate({**_sandbox_node_json(), "idempotent": False})
    assert node.idempotent is False

    agent_node = PipelineGraphNode.model_validate(
        {
            "id": uuid.uuid4(),
            "node_type": "agent",
            "position": {"x": 0, "y": 0},
            "agent_id": uuid.uuid4(),
            "idempotent": False,
        }
    )
    assert agent_node.idempotent is False

    legacy = PipelineGraphNode.model_validate(_sandbox_node_json())
    assert legacy.idempotent is True


def test_pipeline_graph_node_stall_detector_round_trip() -> None:
    """FAR-306: PipelineGraphNode carries the opt-in stall-detector fields and
    defaults them safely for legacy nodes."""
    node = PipelineGraphNode.model_validate(
        {
            **_sandbox_node_json(),
            "stall_timeout_seconds": 600,
            "enable_heartbeat": False,
            "watch_log_path": "/home/user/agent.log",
            "stdout_percentage_delta": 0.2,
            "watch_globs": ["*.log", "/home/user/out/*"],
        }
    )
    assert node.stall_timeout_seconds == 600
    assert node.enable_heartbeat is False
    assert node.watch_log_path == "/home/user/agent.log"
    assert node.stdout_percentage_delta == pytest.approx(0.2)
    assert node.watch_globs == ["*.log", "/home/user/out/*"]

    legacy = PipelineGraphNode.model_validate(_sandbox_node_json())
    assert legacy.enable_heartbeat is True
    assert legacy.watch_log_path is None
    assert legacy.stdout_percentage_delta is None
    assert not legacy.watch_globs
    assert legacy.stall_timeout_seconds is None


def test_pipeline_graph_node_stall_detector_bounds() -> None:
    """FAR-306: stdout_percentage_delta is bounded to [0, 1] by Pydantic.
    Pydantic v2 ValidationError is a ValueError subclass."""
    with pytest.raises(ValueError):
        PipelineGraphNode.model_validate({**_sandbox_node_json(), "stdout_percentage_delta": 1.5})


def test_replace_pipeline_graph_round_trips_delivery_sentinel(client: TestClient) -> None:
    """FAR-228 contract round-trip: a sandbox node sent with delivery_sentinel
    is echoed back on the graph-update response (mocked CRUD)."""
    node = _sandbox_node_json()
    node["delivery_sentinel"] = "EMAIL_SENT"
    validation = MagicMock(issues=[])

    with (
        patch(
            "modulo.api.routes.pipelines.replace_pipeline_graph",
            return_value=([node], []),
        ),
        patch(
            "modulo.api.routes.pipelines.GraphValidator.validate_definition",
            return_value=validation,
        ),
        patch(
            "modulo.api.routes.pipelines._resolve_graph_references",
            return_value=([], []),
        ),
        patch("modulo.api.routes.pipelines.get_pipeline", return_value=_make_pipeline()),
        patch("modulo.api.routes.pipelines.set_rls_org"),
        patch("modulo.api.routes.pipelines.set_rls_user_context"),
    ):
        resp = client.patch(
            f"/api/v1/pipelines/{_PIPELINE_ID}/graph",
            json={"nodes": [node], "edges": []},
        )

    assert resp.status_code == 200
    assert resp.json()["nodes"][0]["delivery_sentinel"] == "EMAIL_SENT"


def test_replace_pipeline_graph_round_trips_correction_target(client: TestClient) -> None:
    """FAR-210 MAJOR-2 contract round-trip: a gate edge carrying correction_target
    is accepted by the real endpoint and SURVIVES the wire — the persisted edge
    data (what becomes the snapshot graph_json) carries it, and the response
    echoes it. Without the HitlGateConfig field, Pydantic's extra='ignore'
    silently drops it before model_dump."""
    node_id = uuid.uuid4()
    target_id = uuid.uuid4()
    reject_id = uuid.uuid4()
    correction_id = uuid.uuid4()
    nodes = [
        {"id": str(node_id), "agent_id": str(uuid.uuid4()), "position": {"x": 10, "y": 20}, "connector_binding": None},
        {"id": str(target_id), "agent_id": str(uuid.uuid4()), "position": {"x": 0, "y": 0}, "connector_binding": None},
        {"id": str(reject_id), "agent_id": str(uuid.uuid4()), "position": {"x": 0, "y": 0}, "connector_binding": None},
        {
            "id": str(correction_id),
            "agent_id": str(uuid.uuid4()),
            "position": {"x": 0, "y": 0},
            "connector_binding": None,
        },
    ]
    edges = [
        {
            "source_node_id": str(node_id),
            "target_node_id": str(target_id),
            "edge_type": "normal",
            "hitl_gate_config": {
                "label": "Review",
                "description": "Gate",
                "reject_target": str(reject_id),
                "correction_target": str(correction_id),
                "claim_expiry_minutes": 60,
                "human_only": False,
            },
        }
    ]
    persisted: dict[str, Any] = {}

    async def _fake_replace(session, **kwargs: Any) -> tuple[Any, Any]:
        persisted["edges"] = kwargs.get("edges")
        return (nodes, kwargs.get("edges") or [])

    validation = MagicMock(issues=[])
    with (
        patch("modulo.api.routes.pipelines.replace_pipeline_graph", side_effect=_fake_replace),
        patch("modulo.api.routes.pipelines.GraphValidator.validate_definition", return_value=validation),
        patch("modulo.api.routes.pipelines._resolve_graph_references", return_value=([], [])),
        patch("modulo.api.routes.pipelines.get_pipeline", return_value=_make_pipeline()),
        patch("modulo.api.routes.pipelines.set_rls_org"),
        patch("modulo.api.routes.pipelines.set_rls_user_context"),
    ):
        resp = client.patch(
            f"/api/v1/pipelines/{_PIPELINE_ID}/graph",
            json={"nodes": nodes, "edges": edges},
        )

    assert resp.status_code == 200
    # The persisted edge data (the snapshot graph_json source) carries the field.
    persisted_edge = persisted["edges"][0]
    assert persisted_edge["hitl_gate_config"]["correction_target"] == str(correction_id)
    assert persisted_edge["hitl_gate_config"]["reject_target"] == str(reject_id)
    # The response round-trips it back.
    assert resp.json()["edges"][0]["hitl_gate_config"]["correction_target"] == str(correction_id)


def test_get_pipeline_graph_returns_correction_target(client: TestClient) -> None:
    """FAR-210 MAJOR-2: a reload (GET /graph) of a gate whose hitl_gate_config
    carries correction_target returns it — the field survives the
    PipelineGraphEdge round-trip."""
    node_id = uuid.uuid4()
    correction_id = uuid.uuid4()
    edge = MagicMock()
    edge.id = uuid.uuid4()
    edge.source_node_id = node_id
    edge.target_node_id = uuid.uuid4()
    edge.edge_type = "normal"
    edge.condition_expression = None
    edge.source_port = "out"
    edge.target_port = "in"
    edge.hitl_gate_config = {
        "label": "Review",
        "description": "Gate",
        "reject_target": str(uuid.uuid4()),
        "correction_target": str(correction_id),
        "claim_expiry_minutes": 60,
        "human_only": False,
    }
    nodes = [
        {"id": str(node_id), "agent_id": str(uuid.uuid4()), "position": {"x": 10, "y": 20}, "connector_binding": None}
    ]

    with (
        patch("modulo.api.routes.pipelines.get_pipeline_graph", return_value=(nodes, [edge])),
        patch("modulo.api.routes.pipelines.set_rls_org"),
        patch("modulo.api.routes.pipelines.set_rls_user_context"),
    ):
        resp = client.get(f"/api/v1/pipelines/{_PIPELINE_ID}/graph")

    assert resp.status_code == 200
    assert resp.json()["edges"][0]["hitl_gate_config"]["correction_target"] == str(correction_id)


def test_replace_pipeline_graph_blocks_redact_correct_422(client: TestClient) -> None:
    """FAR-210 Minor-3 (review): the REDACT_CORRECT_BLOCKED -> 422 branch of
    replace_pipeline_graph_endpoint is proven at the ENDPOINT level, not just the
    validator. A graph-save whose guardrail rows carry a 'redact'-action
    guardrail with a 'correction' block is rejected with 422 and the
    REDACT_CORRECT_BLOCKED detail message (mirrors the GUARDRAIL_CAP_EXCEEDED
    coverage). Without the route branch this test fails: the graph would save
    with a 200."""
    node_id = uuid.uuid4()
    target_id = uuid.uuid4()
    nodes = [
        {"id": str(node_id), "agent_id": str(uuid.uuid4()), "position": {"x": 10, "y": 20}, "connector_binding": None},
        {"id": str(target_id), "agent_id": str(uuid.uuid4()), "position": {"x": 0, "y": 0}, "connector_binding": None},
    ]
    edges = [
        {
            "source_node_id": str(node_id),
            "target_node_id": str(target_id),
            "edge_type": "normal",
        }
    ]
    validation = ValidationResult()
    validation.error(
        "REDACT_CORRECT_BLOCKED",
        "Guardrail 'gr_redact' declares a 'correction' block on a 'redact'-action "
        "guardrail — a correction on a redaction guardrail is an exfiltration channel",
    )
    with (
        patch("modulo.api.routes.pipelines.replace_pipeline_graph", return_value=(nodes, edges)),
        patch(
            "modulo.api.routes.pipelines.GraphValidator.validate_definition",
            return_value=validation,
        ),
        patch(
            "modulo.db.crud.guardrail_config.load_pipeline_guardrail_rows",
            return_value=[],
        ),
        patch("modulo.api.routes.pipelines._resolve_graph_references", return_value=([], [])),
        patch("modulo.api.routes.pipelines.get_pipeline", return_value=_make_pipeline()),
        patch("modulo.api.routes.pipelines.set_rls_org"),
        patch("modulo.api.routes.pipelines.set_rls_user_context"),
    ):
        resp = client.patch(
            f"/api/v1/pipelines/{_PIPELINE_ID}/graph",
            json={"nodes": nodes, "edges": edges},
        )

    assert resp.status_code == 422
    assert "exfiltration channel" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# FAR-309 PR A — mutation-time guardrail-binding strip enforcement
# ---------------------------------------------------------------------------


def _guardrail_row(node_id: uuid.UUID) -> SimpleNamespace:
    """A node-bound guardrail eval row (``node_id`` set, org-level rows are
    ``node_id=None``)."""
    return SimpleNamespace(
        id=uuid.uuid4(),
        node_id=node_id,
        name="no-aws-keys",
        eval_type="guardrail",
    )


def test_nonadmin_cannot_strip_guardrail_binding(operator_client: TestClient) -> None:
    """FAR-309 PR A prove-the-fix: a NON-ADMIN (operator) saving a graph that
    REMOVES a node carrying a bound guardrail is denied 403. The enforcement
    lives in the SERVICE LAYER (``replace_pipeline_graph``, under the row
    lock); the route translates the ``GuardrailBindingStripDenied`` it raises
    into a 403. Without the service-layer guard the save would succeed (200) —
    the guardrail-bound node would silently drop its binding."""
    bound_node_id = uuid.uuid4()
    kept_node_id = uuid.uuid4()
    nodes = [{"id": str(kept_node_id), "agent_id": str(uuid.uuid4()), "position": {"x": 0, "y": 0}}]
    edges = []

    denied = GuardrailBindingStripDenied(
        stripped_node_ids=[str(bound_node_id)],
        detail=(
            "Non-admin cannot strip a guardrail binding: removing node(s) "
            + str(bound_node_id)
            + " from the graph would drop a node-bound guardrail. Only an "
            "admin can remove a node that has a bound guardrail."
        ),
    )

    with (
        patch(
            "modulo.api.routes.pipelines.replace_pipeline_graph",
            side_effect=denied,
        ),
        patch("modulo.api.routes.pipelines.GraphValidator.validate_definition", return_value=MagicMock(issues=[])),
        patch("modulo.api.routes.pipelines._resolve_graph_references", return_value=([], [])),
        patch("modulo.api.routes.pipelines.get_pipeline", return_value=_make_pipeline()),
        patch("modulo.api.routes.pipelines.set_rls_org"),
        patch("modulo.api.routes.pipelines.set_rls_user_context"),
        patch(
            "modulo.db.crud.guardrail_config.load_pipeline_guardrail_rows",
            return_value=[_guardrail_row(bound_node_id)],
        ),
    ):
        resp = operator_client.patch(
            f"/api/v1/pipelines/{_PIPELINE_ID}/graph",
            json={"nodes": nodes, "edges": edges},
        )

    assert resp.status_code == 403
    assert "strip a guardrail binding" in resp.json()["detail"]
    assert str(bound_node_id) in resp.json()["detail"]


def test_admin_can_strip_guardrail_binding(client: TestClient) -> None:
    """FAR-309 PR A: an ADMIN may remove a guardrail-bound node from the graph
    (admin owns guardrail management via ``guardrail.manage``)."""
    bound_node_id = uuid.uuid4()
    kept_node_id = uuid.uuid4()
    nodes = [{"id": str(kept_node_id), "agent_id": str(uuid.uuid4()), "position": {"x": 0, "y": 0}}]
    edges = []

    with (
        patch("modulo.api.routes.pipelines.replace_pipeline_graph", return_value=(nodes, edges)),
        patch("modulo.api.routes.pipelines.GraphValidator.validate_definition", return_value=MagicMock(issues=[])),
        patch("modulo.api.routes.pipelines._resolve_graph_references", return_value=([], [])),
        patch("modulo.api.routes.pipelines.get_pipeline", return_value=_make_pipeline()),
        patch("modulo.api.routes.pipelines.set_rls_org"),
        patch("modulo.api.routes.pipelines.set_rls_user_context"),
        patch(
            "modulo.db.crud.guardrail_config.load_pipeline_guardrail_rows",
            return_value=[_guardrail_row(bound_node_id)],
        ),
    ):
        resp = client.patch(
            f"/api/v1/pipelines/{_PIPELINE_ID}/graph",
            json={"nodes": nodes, "edges": edges},
        )

    assert resp.status_code == 200


def test_nonadmin_cannot_strip_guardrail_binding_via_update_graph_json(
    operator_client: TestClient,
) -> None:
    """FAR-309 PR A review bypass-path #1 (MAJOR): a NON-ADMIN can previously
    bypass the graph-save strip guard by using ``PATCH /api/v1/pipelines/{id}``
    with ``graph_json`` (``update_pipeline_endpoint``, operator-level
    ``pipeline.update``), which replaced the graph WITHOUT the check. The
    service-layer guard now covers this path too: the route translates the
    ``GuardrailBindingStripDenied`` raised by ``replace_pipeline_graph`` into
    403. This test FAILS without the service-layer guard (the save would
    succeed 200)."""
    bound_node_id = uuid.uuid4()
    kept_node_id = uuid.uuid4()
    node = {"id": str(kept_node_id), "agent_id": str(uuid.uuid4()), "position": {"x": 0, "y": 0}}
    denied = GuardrailBindingStripDenied(
        stripped_node_ids=[str(bound_node_id)],
        detail=(
            "Non-admin cannot strip a guardrail binding: removing node(s) "
            + str(bound_node_id)
            + " from the graph would drop a node-bound guardrail. Only an "
            "admin can remove a node that has a bound guardrail."
        ),
    )
    pipeline = _make_pipeline()

    with (
        patch("modulo.api.routes.pipelines.get_pipeline", return_value=pipeline),
        patch("modulo.api.routes.pipelines.find_connector_team_mismatches", new=AsyncMock(return_value=[])),
        patch("modulo.api.routes.pipelines._resolve_graph_references", new=AsyncMock(return_value=([], []))),
        patch("modulo.api.routes.pipelines.replace_pipeline_graph", side_effect=denied),
        patch("modulo.api.routes.pipelines.update_pipeline", return_value=pipeline),
        patch("modulo.api.routes.pipelines.set_rls_org"),
        patch("modulo.api.routes.pipelines.set_rls_user_context"),
    ):
        resp = operator_client.patch(
            f"/api/v1/pipelines/{_PIPELINE_ID}",
            json={"graph_json": {"nodes": [node], "edges": []}},
        )

    assert resp.status_code == 403
    assert "strip a guardrail binding" in resp.json()["detail"]
    assert str(bound_node_id) in resp.json()["detail"]


def test_admin_can_strip_guardrail_binding_via_update_graph_json(client: TestClient) -> None:
    """FAR-309 PR A review: an ADMIN may strip a guardrail-bound node via the
    ``PATCH /{id}`` ``graph_json`` path (admin owns guardrail management)."""
    kept_node_id = uuid.uuid4()
    node = {"id": str(kept_node_id), "agent_id": str(uuid.uuid4()), "position": {"x": 0, "y": 0}}
    pipeline = _make_pipeline()

    with (
        patch("modulo.api.routes.pipelines.get_pipeline", return_value=pipeline),
        patch("modulo.api.routes.pipelines.find_connector_team_mismatches", new=AsyncMock(return_value=[])),
        patch("modulo.api.routes.pipelines._resolve_graph_references", new=AsyncMock(return_value=([], []))),
        patch("modulo.api.routes.pipelines.replace_pipeline_graph", return_value=([node], [])),
        patch("modulo.api.routes.pipelines.update_pipeline", return_value=pipeline),
        patch("modulo.api.routes.pipelines.set_rls_org"),
        patch("modulo.api.routes.pipelines.set_rls_user_context"),
    ):
        resp = client.patch(
            f"/api/v1/pipelines/{_PIPELINE_ID}",
            json={"graph_json": {"nodes": [node], "edges": []}},
        )

    assert resp.status_code == 200


def test_nonadmin_cannot_strip_guardrail_binding_via_snapshot_rollback(
    operator_client: TestClient,
) -> None:
    """FAR-309 PR A review bypass-path #2 (MAJOR): a NON-ADMIN can previously
    bypass the graph-save strip guard by rolling back to a snapshot whose
    graph LACKS a currently guardrail-bound node
    (``rollback_snapshot_endpoint``, operator-level ``pipeline.graph.update``),
    which overwrote the graph WITHOUT the check. The service-layer guard now
    covers this path too: the route translates the ``GuardrailBindingStripDenied``
    raised by ``rollback_to_snapshot`` into 403. This test FAILS without the
    service-layer guard (the rollback would succeed 200)."""
    bound_node_id = uuid.uuid4()
    snapshot_id = uuid.uuid4()
    denied = GuardrailBindingStripDenied(
        stripped_node_ids=[str(bound_node_id)],
        detail=(
            "Non-admin cannot strip a guardrail binding: removing node(s) "
            + str(bound_node_id)
            + " from the graph would drop a node-bound guardrail. Only an "
            "admin can remove a node that has a bound guardrail."
        ),
    )

    with (
        patch("modulo.api.routes.pipelines.rollback_to_snapshot", side_effect=denied),
        patch("modulo.api.routes.pipelines.set_rls_org"),
        patch("modulo.api.routes.pipelines.set_rls_user_context"),
    ):
        resp = operator_client.post(
            f"/api/v1/pipelines/{_PIPELINE_ID}/snapshots/{snapshot_id}/rollback",
        )

    assert resp.status_code == 403
    assert "strip a guardrail binding" in resp.json()["detail"]
    assert str(bound_node_id) in resp.json()["detail"]


def test_admin_can_strip_guardrail_binding_via_snapshot_rollback(client: TestClient) -> None:
    """FAR-309 PR A review: an ADMIN may roll back to a snapshot that drops a
    guardrail-bound node (admin owns guardrail management)."""
    snapshot_id = uuid.uuid4()
    new_snapshot = MagicMock()
    new_snapshot.id = uuid.uuid4()
    new_snapshot.pipeline_id = _PIPELINE_ID
    new_snapshot.snapshot_version = 2
    new_snapshot.tag = None
    new_snapshot.notes = None
    new_snapshot.created_at = _NOW
    new_snapshot.account_id = uuid.uuid4()
    new_snapshot.version_kind = "run"
    new_snapshot.created_kind = "run"
    new_snapshot.draft = False
    new_snapshot.channel = "none"

    with (
        patch("modulo.api.routes.pipelines.rollback_to_snapshot", return_value=new_snapshot),
        patch("modulo.api.routes.pipelines.set_rls_org"),
        patch("modulo.api.routes.pipelines.set_rls_user_context"),
    ):
        resp = client.post(
            f"/api/v1/pipelines/{_PIPELINE_ID}/snapshots/{snapshot_id}/rollback",
        )

    assert resp.status_code == 200


def test_nonadmin_unrelated_graph_changes_allowed(operator_client: TestClient) -> None:
    """FAR-309 PR A field-level scope: a NON-ADMIN making unrelated graph
    changes (edge rewiring, editing other nodes) while KEEPING the
    guardrail-bound node is allowed — only guardrail-binding removal is
    protected."""
    bound_node_id = uuid.uuid4()
    kept_node_id = uuid.uuid4()
    nodes = [
        {"id": str(bound_node_id), "agent_id": str(uuid.uuid4()), "position": {"x": 0, "y": 0}},
        {"id": str(kept_node_id), "agent_id": str(uuid.uuid4()), "position": {"x": 10, "y": 0}},
    ]
    edges = [
        {
            "source_node_id": str(bound_node_id),
            "target_node_id": str(kept_node_id),
            "edge_type": "normal",
        }
    ]

    with (
        patch("modulo.api.routes.pipelines.replace_pipeline_graph", return_value=(nodes, edges)),
        patch("modulo.api.routes.pipelines.GraphValidator.validate_definition", return_value=MagicMock(issues=[])),
        patch("modulo.api.routes.pipelines._resolve_graph_references", return_value=([], [])),
        patch("modulo.api.routes.pipelines.get_pipeline", return_value=_make_pipeline()),
        patch("modulo.api.routes.pipelines.set_rls_org"),
        patch("modulo.api.routes.pipelines.set_rls_user_context"),
        patch(
            "modulo.db.crud.guardrail_config.load_pipeline_guardrail_rows",
            return_value=[_guardrail_row(bound_node_id)],
        ),
    ):
        resp = operator_client.patch(
            f"/api/v1/pipelines/{_PIPELINE_ID}/graph",
            json={"nodes": nodes, "edges": edges},
        )

    assert resp.status_code == 200


def test_nonadmin_removing_unbound_node_allowed(operator_client: TestClient) -> None:
    """FAR-309 PR A field-level scope: removing a node with NO bound guardrail
    is an ordinary graph change — a non-admin may do it. The enforcement
    protects only guardrail-bound nodes."""
    kept_node_id = uuid.uuid4()
    nodes = [{"id": str(kept_node_id), "agent_id": str(uuid.uuid4()), "position": {"x": 0, "y": 0}}]
    edges = []

    with (
        patch("modulo.api.routes.pipelines.replace_pipeline_graph", return_value=(nodes, edges)),
        patch("modulo.api.routes.pipelines.GraphValidator.validate_definition", return_value=MagicMock(issues=[])),
        patch("modulo.api.routes.pipelines._resolve_graph_references", return_value=([], [])),
        patch("modulo.api.routes.pipelines.get_pipeline", return_value=_make_pipeline()),
        patch("modulo.api.routes.pipelines.set_rls_org"),
        patch("modulo.api.routes.pipelines.set_rls_user_context"),
        # A guardrail bound to a DIFFERENT node (kept in the graph) — the removed
        # node has none, so removal is allowed.
        patch(
            "modulo.db.crud.guardrail_config.load_pipeline_guardrail_rows",
            return_value=[_guardrail_row(kept_node_id)],
        ),
    ):
        resp = operator_client.patch(
            f"/api/v1/pipelines/{_PIPELINE_ID}/graph",
            json={"nodes": nodes, "edges": edges},
        )

    assert resp.status_code == 200


def test_replace_pipeline_graph_rejects_excessive_node_count(client: TestClient) -> None:
    nodes = [
        {"id": str(uuid.uuid4()), "agent_id": str(uuid.uuid4()), "position": {"x": i * 10, "y": 0}} for i in range(501)
    ]
    resp = client.patch(
        f"/api/v1/pipelines/{_PIPELINE_ID}/graph",
        json={"nodes": nodes, "edges": []},
    )
    assert resp.status_code == 422
    body = resp.json()
    errors = body.get("detail", body.get("error", {}).get("detail", ""))
    assert "exceeds maximum" in (errors if isinstance(errors, str) else str(errors))


def test_replace_pipeline_graph_rejects_excessive_edge_count(client: TestClient) -> None:
    node_a = uuid.uuid4()
    node_b = uuid.uuid4()
    edges = [{"source_node_id": str(node_a), "target_node_id": str(node_b), "edge_type": "normal"} for _ in range(1001)]
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
    body = resp.json()
    errors = body.get("detail", body.get("error", {}).get("detail", ""))
    assert "exceeds maximum" in (errors if isinstance(errors, str) else str(errors))


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
def test_replace_pipeline_graph_rejects_node_type_conflicts(client: TestClient, node: dict[str, object]) -> None:
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


# ---------------------------------------------------------------------------
# Cross-team connector binding enforcement (PRD §9.3)
# ---------------------------------------------------------------------------


def _graph_node_with_connector_binding(connector_id: uuid.UUID, node_id: uuid.UUID | None = None) -> dict[str, object]:
    return {
        "id": str(node_id or uuid.uuid4()),
        "node_type": "agent",
        "agent_id": str(uuid.uuid4()),
        "position": {"x": 10, "y": 20},
        "connector_binding": {"type": "test", "instance_id": str(connector_id)},
    }


def test_replace_graph_blocks_cross_team_connector_binding(client: TestClient) -> None:
    connector_id = uuid.uuid4()
    team_a = uuid.uuid4()
    team_b = uuid.uuid4()
    pipeline = _make_pipeline()
    pipeline.owner_team_id = team_a
    node = _graph_node_with_connector_binding(connector_id)
    mismatch = MagicMock(
        connector_id=connector_id,
        connector_name="eng-db",
        connector_owner_team_id=team_b,
        pipeline_owner_team_id=team_a,
        node_id=str(node["id"]),
    )

    with (
        patch("modulo.api.routes.pipelines.get_pipeline", return_value=pipeline),
        patch(
            "modulo.api.routes.pipelines.find_connector_team_mismatches",
            new=AsyncMock(return_value=[mismatch]),
        ) as find_mismatches,
        patch("modulo.api.routes.pipelines.replace_pipeline_graph") as replace_graph,
        patch("modulo.api.routes.pipelines.set_rls_org"),
        patch("modulo.api.routes.pipelines.set_rls_user_context"),
    ):
        resp = client.patch(
            f"/api/v1/pipelines/{_PIPELINE_ID}/graph",
            json={"nodes": [node], "edges": []},
        )

    assert resp.status_code == 409
    assert "connector_team_mismatch" in resp.json()["detail"]
    replace_graph.assert_not_awaited()
    assert find_mismatches.await_args.kwargs["pipeline_owner_team_id"] == team_a


def test_replace_graph_allows_same_team_connector_binding(client: TestClient) -> None:
    connector_id = uuid.uuid4()
    team_a = uuid.uuid4()
    pipeline = _make_pipeline()
    pipeline.owner_team_id = team_a
    node = _graph_node_with_connector_binding(connector_id)
    validation = MagicMock(issues=[])

    with (
        patch("modulo.api.routes.pipelines.get_pipeline", return_value=pipeline),
        patch(
            "modulo.api.routes.pipelines.find_connector_team_mismatches",
            new=AsyncMock(return_value=[]),
        ),
        patch("modulo.api.routes.pipelines.replace_pipeline_graph", return_value=([node], [])),
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
            json={"nodes": [node], "edges": []},
        )

    assert resp.status_code == 200


def test_replace_graph_org_pipeline_pinning_team_private_model_backend_is_409(client: TestClient) -> None:
    """An org-owned pipeline (owner_team_id=None) must NOT skip model-backend team scope.

    PRD §9.3: an org pipeline pinning a team-private model backend leaks the
    private backend to the whole org. Mirror the connector rule: the mismatch
    check runs even when the pipeline has no owning team.
    """
    agent_id = uuid.uuid4()
    backend_id = uuid.uuid4()
    team_owner = uuid.uuid4()
    pipeline = _make_pipeline()  # owner_team_id=None → org-owned
    node = {
        "id": str(uuid.uuid4()),
        "node_type": "agent",
        "agent_id": str(agent_id),
        "position": {"x": 10, "y": 20},
    }
    agent = MagicMock(
        id=agent_id,
        input_schema_id=uuid.uuid4(),
        output_schema_id=uuid.uuid4(),
        model_backend_id=backend_id,
    )
    backend = MagicMock(
        id=backend_id,
        visibility="team",
        owner_team_id=team_owner,
        organisation_id=_ORG_ID,
    )
    backend.name = "eng-llm"

    session = _make_mock_session()
    agent_result = MagicMock()
    agent_result.scalars.return_value = [agent]
    backend_result = MagicMock()
    backend_result.scalars.return_value.all.return_value = [backend]
    authz_result = MagicMock()
    authz_result.scalar_one_or_none.return_value = None

    async def _execute(query, *args: object, **kwargs: object) -> MagicMock:
        table = str(query).split("\n", 1)[0].lower()
        if "organisations.authz_enforce" in table:
            return authz_result
        if "model_backends" in table:
            return backend_result
        return agent_result

    session.execute = AsyncMock(side_effect=_execute)

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield session

    app.dependency_overrides[get_db_session] = override_session
    with (
        patch("modulo.api.routes.pipelines.get_pipeline", return_value=pipeline),
        patch(
            "modulo.api.routes.pipelines.find_connector_team_mismatches",
            new=AsyncMock(return_value=[]),
        ),
        patch("modulo.api.routes.pipelines.replace_pipeline_graph"),
        patch("modulo.api.routes.pipelines.set_rls_org"),
        patch("modulo.api.routes.pipelines.set_rls_user_context"),
    ):
        resp = client.patch(
            f"/api/v1/pipelines/{_PIPELINE_ID}/graph",
            json={"nodes": [node], "edges": []},
        )

    assert resp.status_code == 409
    assert "model_backend_team_mismatch" in resp.json()["detail"]
    assert "owned by team None" in resp.json()["detail"]


def test_replace_graph_missing_pipeline_returns_404(client: TestClient) -> None:
    connector_id = uuid.uuid4()
    node = _graph_node_with_connector_binding(connector_id)

    with (
        patch("modulo.api.routes.pipelines.get_pipeline", return_value=None),
        patch("modulo.api.routes.pipelines.replace_pipeline_graph") as replace_graph,
        patch("modulo.api.routes.pipelines.set_rls_org"),
        patch("modulo.api.routes.pipelines.set_rls_user_context"),
    ):
        resp = client.patch(
            f"/api/v1/pipelines/{_PIPELINE_ID}/graph",
            json={"nodes": [node], "edges": []},
        )

    assert resp.status_code == 404
    replace_graph.assert_not_awaited()


def test_update_pipeline_with_graph_blocks_cross_team_connector_binding(client: TestClient) -> None:
    connector_id = uuid.uuid4()
    team_a = uuid.uuid4()
    team_b = uuid.uuid4()
    pipeline = _make_pipeline()
    pipeline.owner_team_id = team_a
    node = _graph_node_with_connector_binding(connector_id)
    mismatch = MagicMock(
        connector_id=connector_id,
        connector_name="eng-db",
        connector_owner_team_id=team_b,
        pipeline_owner_team_id=team_a,
        node_id=str(node["id"]),
    )

    with (
        patch("modulo.api.routes.pipelines.get_pipeline", return_value=pipeline),
        patch(
            "modulo.api.routes.pipelines.find_connector_team_mismatches",
            new=AsyncMock(return_value=[mismatch]),
        ),
        patch("modulo.api.routes.pipelines.replace_pipeline_graph") as replace_graph,
        patch("modulo.api.routes.pipelines.update_pipeline") as update_pipeline,
        patch("modulo.api.routes.pipelines.set_rls_org"),
        patch("modulo.api.routes.pipelines.set_rls_user_context"),
    ):
        resp = client.patch(
            f"/api/v1/pipelines/{_PIPELINE_ID}",
            json={"graph_json": {"nodes": [node], "edges": []}},
        )

    assert resp.status_code == 409
    assert "connector_team_mismatch" in resp.json()["detail"]
    replace_graph.assert_not_awaited()
    update_pipeline.assert_not_awaited()


def test_update_pipeline_with_graph_uses_effective_owner_team(client: TestClient) -> None:
    connector_id = uuid.uuid4()
    team_a = uuid.uuid4()
    team_b = uuid.uuid4()
    pipeline = _make_pipeline()
    pipeline.owner_team_id = team_a
    node = _graph_node_with_connector_binding(connector_id)
    validation = MagicMock(issues=[])

    with (
        patch("modulo.api.routes.pipelines.get_pipeline", return_value=pipeline),
        patch(
            "modulo.api.routes.pipelines.find_connector_team_mismatches",
            new=AsyncMock(return_value=[]),
        ) as find_mismatches,
        patch(
            "modulo.api.routes.pipelines._resolve_graph_references",
            new=AsyncMock(return_value=([], [])),
        ) as resolve_refs,
        patch("modulo.api.routes.pipelines.replace_pipeline_graph", return_value=([node], [])),
        patch(
            "modulo.api.routes.pipelines.GraphValidator.validate_definition",
            return_value=validation,
        ),
        patch("modulo.api.routes.pipelines.update_pipeline", return_value=pipeline),
        patch("modulo.api.routes.pipelines.set_rls_org"),
        patch("modulo.api.routes.pipelines.set_rls_user_context"),
    ):
        resp = client.patch(
            f"/api/v1/pipelines/{_PIPELINE_ID}",
            json={
                "owner_team_id": str(team_b),
                "graph_json": {"nodes": [node], "edges": []},
            },
        )

    assert resp.status_code == 200
    assert find_mismatches.await_args.kwargs["pipeline_owner_team_id"] == team_b
    assert resolve_refs.await_args.kwargs["pipeline_owner_team_id"] == team_b


def test_update_pipeline_with_graph_blocks_cross_team_model_backend_binding(client: TestClient) -> None:
    connector_id = uuid.uuid4()
    team_a = uuid.uuid4()
    team_b = uuid.uuid4()
    pipeline = _make_pipeline()
    pipeline.owner_team_id = team_a
    node = _graph_node_with_connector_binding(connector_id)

    with (
        patch("modulo.api.routes.pipelines.get_pipeline", return_value=pipeline),
        patch(
            "modulo.api.routes.pipelines.find_connector_team_mismatches",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "modulo.api.routes.pipelines._resolve_graph_references",
            new=AsyncMock(
                side_effect=HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="model_backend_team_mismatch: model backend 'eng-llm' is team-private "
                    f"(owner team {team_a}) but pipeline is owned by team {team_b}",
                )
            ),
        ) as resolve_refs,
        patch("modulo.api.routes.pipelines.replace_pipeline_graph", return_value=([node], [])),
        patch("modulo.api.routes.pipelines.update_pipeline", return_value=pipeline),
        patch("modulo.api.routes.pipelines.set_rls_org"),
        patch("modulo.api.routes.pipelines.set_rls_user_context"),
    ):
        resp = client.patch(
            f"/api/v1/pipelines/{_PIPELINE_ID}",
            json={
                "owner_team_id": str(team_b),
                "graph_json": {"nodes": [node], "edges": []},
            },
        )

    assert resp.status_code == 409
    assert resolve_refs.await_args.kwargs["pipeline_owner_team_id"] == team_b
    assert "model_backend_team_mismatch" in resp.json()["detail"]


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
    backend_result = MagicMock()
    backend_result.scalars.return_value.all.return_value = []
    session = configure_mock_session(AsyncMock())
    session.execute = AsyncMock(side_effect=[agent_result, schema_result, backend_result])
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
    session = configure_mock_session(AsyncMock())
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
    session = configure_mock_session(AsyncMock())
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
    assert "Unknown schema IDs for this organisation" in exc_info.value.detail


# ---------------------------------------------------------------------------
# PipelineGraphNode model — FAR-295 idempotent flag (all executor types)
# ---------------------------------------------------------------------------


def _minimal_node(**overrides: Any) -> dict[str, Any]:
    node = {
        "id": str(uuid.uuid4()),
        "node_type": "agent",
        "agent_id": str(uuid.uuid4()),
        "position": {"x": 0.0, "y": 0.0},
    }
    node.update(overrides)
    return node


def test_graph_node_idempotent_defaults_to_true() -> None:
    """The idempotent flag defaults to true on EVERY executor type — a node
    author must opt out explicitly (idempotent=false) to suppress retries."""
    assert PipelineGraphNode.model_validate(_minimal_node()).idempotent is True
    common = {"id": str(uuid.uuid4()), "position": {"x": 0.0, "y": 0.0}}
    typed_nodes = [
        {**common, "node_type": "agent", "agent_id": str(uuid.uuid4())},
        {**common, "node_type": "manual", "label": "manual step", "output_schema_id": str(uuid.uuid4())},
        {**common, "node_type": "composite", "composite_ref": str(uuid.uuid4())},
        {
            **common,
            "node_type": "sandbox_agent",
            "template_id": "opencode",
            "agent_command": "opencode run",
            "agent_prompt": "do the thing",
        },
    ]
    for node_dict in typed_nodes:
        typed = PipelineGraphNode.model_validate(node_dict)
        assert typed.idempotent is True, f"{node_dict['node_type']} should default idempotent to true"


def test_graph_node_idempotent_explicit_false_and_roundtrip() -> None:
    """idempotent=false is accepted and survives the JSON round-trip used by
    the graph save path (node.model_dump(mode='json'))."""
    node = PipelineGraphNode.model_validate(_minimal_node(idempotent=False))
    assert node.idempotent is False
    dumped = node.model_dump(mode="json")
    assert dumped["idempotent"] is False
    # Re-validating the dumped dict round-trips the flag.
    assert PipelineGraphNode.model_validate(dumped).idempotent is False


# ---------------------------------------------------------------------------
# PATCH /api/v1/pipelines/{id}
# ---------------------------------------------------------------------------


def test_update_pipeline_returns_200(client: TestClient) -> None:
    pipeline = _make_pipeline()
    pipeline.name = "Updated"

    with (
        patch("modulo.api.routes.pipelines.update_pipeline", return_value=pipeline),
        patch("modulo.api.routes.pipelines.get_pipeline", new=AsyncMock(return_value=pipeline)),
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
        patch("modulo.api.routes.pipelines.get_pipeline", new=AsyncMock(return_value=None)),
        patch("modulo.api.routes.pipelines.set_rls_org"),
        patch("modulo.api.routes.pipelines.set_rls_user_context"),
    ):
        resp = client.patch(f"/api/v1/pipelines/{uuid.uuid4()}", json={"name": "x"})

    assert resp.status_code == 404


def test_update_pipeline_rejects_null_stale_run_timeout(client: TestClient) -> None:
    resp = client.patch(
        f"/api/v1/pipelines/{_PIPELINE_ID}",
        json={"stale_run_timeout_minutes": None},
    )

    assert resp.status_code == 422


def test_update_pipeline_rejects_null_max_duration(client: TestClient) -> None:
    resp = client.patch(
        f"/api/v1/pipelines/{_PIPELINE_ID}",
        json={"max_duration_seconds": None},
    )

    assert resp.status_code == 422


def test_update_pipeline_rejects_null_node_timeout(client: TestClient) -> None:
    resp = client.patch(
        f"/api/v1/pipelines/{_PIPELINE_ID}",
        json={"node_timeout_seconds": None},
    )

    assert resp.status_code == 422


def test_update_pipeline_rejects_null_lock_wait_timeout(client: TestClient) -> None:
    resp = client.patch(
        f"/api/v1/pipelines/{_PIPELINE_ID}",
        json={"lock_wait_timeout_seconds": None},
    )

    assert resp.status_code == 422


def test_update_pipeline_accepts_stale_run_timeout(client: TestClient) -> None:
    pipeline = _make_pipeline()
    pipeline.stale_run_timeout_minutes = 45

    with (
        patch("modulo.api.routes.pipelines.update_pipeline", return_value=pipeline) as update,
        patch("modulo.api.routes.pipelines.get_pipeline", new=AsyncMock(return_value=pipeline)),
        patch("modulo.api.routes.pipelines.set_rls_org"),
        patch("modulo.api.routes.pipelines.set_rls_user_context"),
    ):
        resp = client.patch(
            f"/api/v1/pipelines/{_PIPELINE_ID}",
            json={"stale_run_timeout_minutes": 45},
        )

    assert resp.status_code == 200
    assert resp.json()["stale_run_timeout_minutes"] == 45
    assert update.await_args.args[2]["stale_run_timeout_minutes"] == 45


def test_update_pipeline_openapi_schema_not_nullable() -> None:
    patch_schema = app.openapi()["components"]["schemas"]["PipelineUpdate"]["properties"]["stale_run_timeout_minutes"]

    assert patch_schema["type"] == "integer"
    assert "anyOf" not in patch_schema
    assert patch_schema.get("nullable") is not True


# ---------------------------------------------------------------------------
# DELETE /api/v1/pipelines/{id}
# ---------------------------------------------------------------------------


def test_delete_pipeline_returns_204(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.pipelines.soft_delete_pipeline", return_value=_make_pipeline()),
        patch("modulo.api.routes.pipelines.set_rls_org"),
        patch("modulo.api.routes.pipelines.set_rls_user_context"),
    ):
        resp = client.delete(f"/api/v1/pipelines/{_PIPELINE_ID}")

    assert resp.status_code == 204


def test_delete_pipeline_not_found_returns_404(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.pipelines.soft_delete_pipeline", return_value=None),
        patch("modulo.api.routes.pipelines.set_rls_org"),
        patch("modulo.api.routes.pipelines.set_rls_user_context"),
    ):
        resp = client.delete(f"/api/v1/pipelines/{uuid.uuid4()}")

    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/v1/pipelines/{id}/clone
# ---------------------------------------------------------------------------


def test_clone_pipeline_returns_201(client: TestClient) -> None:
    source = _make_pipeline()
    cloned = _make_pipeline()
    cloned.name = "Copy of Test Pipeline"
    cloned.id = uuid.uuid4()

    with (
        patch("modulo.api.routes.pipelines.get_pipeline", return_value=source),
        patch(
            "modulo.api.routes.pipelines.check_pipeline_name_available",
            return_value=True,
        ),
        patch("modulo.api.routes.pipelines.clone_pipeline", return_value=cloned) as mock_clone,
        patch("modulo.api.routes.pipelines.set_rls_org"),
        patch("modulo.api.routes.pipelines.set_rls_user_context"),
        patch("modulo.api.routes.pipelines.append_audit_event", return_value=None),
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
        account_id=_USER_ID,
        org_role="admin",
        new_name=None,
    )


def test_clone_pipeline_with_custom_name(client: TestClient) -> None:
    source = _make_pipeline()
    cloned = _make_pipeline()
    cloned.name = "My Custom Clone"
    cloned.id = uuid.uuid4()

    with (
        patch("modulo.api.routes.pipelines.get_pipeline", return_value=source),
        patch(
            "modulo.api.routes.pipelines.check_pipeline_name_available",
            return_value=True,
        ),
        patch("modulo.api.routes.pipelines.clone_pipeline", return_value=cloned) as mock_clone,
        patch("modulo.api.routes.pipelines.set_rls_org"),
        patch("modulo.api.routes.pipelines.set_rls_user_context"),
        patch("modulo.api.routes.pipelines.append_audit_event", return_value=None),
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
        account_id=_USER_ID,
        org_role="admin",
        new_name="My Custom Clone",
    )


def test_clone_pipeline_not_found_returns_404(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.pipelines.get_pipeline", return_value=None),
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


# ---------------------------------------------------------------------------
# Pipeline folders (PRD §8.4 — org-scoped nested folders)
# ---------------------------------------------------------------------------


def _make_folder(parent_id: uuid.UUID | None = None) -> MagicMock:
    f = MagicMock()
    f.id = uuid.uuid4()
    f.organisation_id = _ORG_ID
    f.name = "QA Folder"
    f.parent_id = parent_id
    f.sort_order = 0
    f.account_id = _USER_ID
    f.created_at = _NOW
    f.updated_at = _NOW
    return f


def _folder_parent_chain_session(parent_chain: dict[uuid.UUID, uuid.UUID | None]) -> AsyncMock:
    """A mock session whose `execute` resolves folder lookups from a chain map.

    ``parent_chain`` maps folder_id -> parent_id (None for a top-level folder);
    presence in the map means the folder exists in the caller's org. An
    ``id``-projection query (the org-scope existence check) returns the folder
    id for an in-org folder and ``None`` for an unknown / other-org folder
    (RLS); a ``parent_id``-projection query returns that folder's parent. This
    lets the folder CRUD cycle/depth/existence validation walk the real code
    path without a database.

    NOTE: routing between the two query shapes depends on the compiled SQL of
    the shared ``folder_tree`` validators projecting exactly ``parent_id``
    (vs ``id``) — if a validator ever projects another column, extend the
    ``"parent_id" in compiled.string`` discrimination here.
    """
    session = AsyncMock()

    def _execute(stmt: object, *args: object, **kwargs: object) -> MagicMock:
        compiled = stmt.compile()  # type: ignore[attr-defined]
        folder_id = next((v for v in compiled.params.values() if isinstance(v, uuid.UUID)), None)
        result = MagicMock()
        if "parent_id" in compiled.string:
            result.scalar_one_or_none.return_value = parent_chain.get(folder_id)
        else:
            result.scalar_one_or_none.return_value = folder_id if folder_id in parent_chain else None
        return result

    session.execute = AsyncMock(side_effect=_execute)
    session.add = MagicMock()
    return session


class TestPipelineFolderCyclePrevention:
    """Self-parenting and ancestry-cycle rejection in folder parent updates."""

    def _patch_folder(self, folder_id: uuid.UUID) -> MagicMock:
        folder = MagicMock()
        folder.id = folder_id
        folder.parent_id = None
        return folder

    def test_update_folder_rejects_self_parenting(self) -> None:
        folder_id = uuid.uuid4()
        session = _folder_parent_chain_session({folder_id: None})

        with (
            patch("modulo.db.crud.pipeline_folder.get_folder", return_value=self._patch_folder(folder_id)),
            pytest.raises(ValueError, match="cannot be its own parent"),
        ):
            asyncio.run(update_folder(session, folder_id, {"parent_id": folder_id}))

    def test_update_folder_rejects_ancestry_cycle(self) -> None:
        f1, f2, f3 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        # f2 -> f3 -> f1 ; moving f1 under f2 would create a cycle.
        session = _folder_parent_chain_session({f2: f3, f3: f1, f1: None})

        with (
            patch("modulo.db.crud.pipeline_folder.get_folder", return_value=self._patch_folder(f1)),
            pytest.raises(ValueError, match="descendants"),
        ):
            asyncio.run(update_folder(session, f1, {"parent_id": f2}))

    def test_update_folder_rejects_depth_overflow(self) -> None:
        # A chain of 9 ancestors under the new parent exceeds the depth cap of 8.
        ids = [uuid.uuid4() for _ in range(9)]
        chain = {ids[i]: ids[i + 1] for i in range(8)}
        chain[ids[-1]] = None
        target = uuid.uuid4()
        session = _folder_parent_chain_session(chain)

        with (
            patch("modulo.db.crud.pipeline_folder.get_folder", return_value=self._patch_folder(target)),
            pytest.raises(ValueError, match="nesting depth"),
        ):
            asyncio.run(update_folder(session, target, {"parent_id": ids[0]}))

    def test_update_folder_allows_valid_parent_change(self) -> None:
        folder_id = uuid.uuid4()
        parent_id = uuid.uuid4()
        session = _folder_parent_chain_session({parent_id: None})
        folder = MagicMock()
        folder.parent_id = None

        with (
            patch("modulo.db.crud.pipeline_folder.get_folder", return_value=folder),
            patch("modulo.db.crud.pipeline_folder.apply_updates"),
        ):
            result = asyncio.run(update_folder(session, folder_id, {"parent_id": parent_id}))
        assert result is folder

    def test_update_folder_rejects_cross_org_parent(self) -> None:
        """A parent_id resolving to no folder in the caller's org (RLS returns
        None) must be rejected — it would corrupt the org-scoped tree and
        expose a CASCADE tenant-boundary data-loss path."""
        folder_id = uuid.uuid4()
        foreign_parent_id = uuid.uuid4()
        session = _folder_parent_chain_session({})  # foreign parent is not in this org

        with (
            patch("modulo.db.crud.pipeline_folder.get_folder", return_value=self._patch_folder(folder_id)),
            pytest.raises(ValueError, match="Parent folder not found"),
        ):
            asyncio.run(update_folder(session, folder_id, {"parent_id": foreign_parent_id}))

    def test_create_folder_rejects_cross_org_parent(self) -> None:
        foreign_parent_id = uuid.uuid4()
        session = _folder_parent_chain_session({})  # foreign parent is not in this org

        with pytest.raises(ValueError, match="Parent folder not found"):
            asyncio.run(
                create_folder(
                    session,
                    org_id=_ORG_ID,
                    name="QA Folder",
                    account_id=_USER_ID,
                    parent_id=foreign_parent_id,
                )
            )

    def test_create_folder_rejects_depth_overflow(self) -> None:
        """create_folder must enforce the same depth cap as update — a chain of
        9 ancestors under the new parent exceeds MAX_FOLDER_DEPTH (8)."""
        ids = [uuid.uuid4() for _ in range(9)]
        chain = {ids[i]: ids[i + 1] for i in range(8)}
        chain[ids[-1]] = None
        session = _folder_parent_chain_session(chain)

        with pytest.raises(ValueError, match="nesting depth"):
            asyncio.run(
                create_folder(
                    session,
                    org_id=_ORG_ID,
                    name="QA Folder",
                    account_id=_USER_ID,
                    parent_id=ids[0],
                )
            )

    def test_create_folder_allows_valid_parent(self) -> None:
        parent_id = uuid.uuid4()
        session = _folder_parent_chain_session({parent_id: None})

        folder = asyncio.run(
            create_folder(
                session,
                org_id=_ORG_ID,
                name="QA Folder",
                account_id=_USER_ID,
                parent_id=parent_id,
            )
        )
        assert folder.parent_id == parent_id
        assert folder.organisation_id == _ORG_ID


class TestPipelineFolderEndpoints:
    """Dedicated endpoint coverage for folder CRUD + pipeline move (PRD §8.4)."""

    def test_list_folders_returns_200(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.pipeline_folders.list_folders", return_value=[_make_folder()]),
            patch("modulo.api.routes.pipeline_folders.set_rls_org"),
            patch("modulo.api.routes.pipeline_folders.set_rls_user_context"),
        ):
            resp = client.get("/api/v1/pipeline-folders")

        assert resp.status_code == 200
        assert len(resp.json()) == 1

    def test_create_folder_returns_201(self, client: TestClient) -> None:
        folder = _make_folder()
        with (
            patch("modulo.api.routes.pipeline_folders.create_folder", return_value=folder),
            patch("modulo.api.routes.pipeline_folders.set_rls_org"),
            patch("modulo.api.routes.pipeline_folders.set_rls_user_context"),
        ):
            resp = client.post("/api/v1/pipeline-folders", json={"name": "QA Folder"})

        assert resp.status_code == 201
        assert resp.json()["name"] == "QA Folder"

    def test_create_folder_rejects_invalid_parent_returns_422(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.pipeline_folders.create_folder",
                side_effect=ValueError("Folder nesting depth would exceed 8 levels"),
            ),
            patch("modulo.api.routes.pipeline_folders.set_rls_org"),
            patch("modulo.api.routes.pipeline_folders.set_rls_user_context"),
        ):
            resp = client.post(
                "/api/v1/pipeline-folders",
                json={"name": "Deep", "parent_id": str(uuid.uuid4())},
            )

        assert resp.status_code == 422

    def test_update_folder_returns_200(self, client: TestClient) -> None:
        folder = _make_folder()
        folder.name = "Renamed"
        with (
            patch("modulo.api.routes.pipeline_folders.update_folder", return_value=folder),
            patch("modulo.api.routes.pipeline_folders.set_rls_org"),
            patch("modulo.api.routes.pipeline_folders.set_rls_user_context"),
        ):
            resp = client.patch(f"/api/v1/pipeline-folders/{folder.id}", json={"name": "Renamed"})

        assert resp.status_code == 200
        assert resp.json()["name"] == "Renamed"

    def test_update_folder_self_parent_returns_422(self, client: TestClient) -> None:
        folder_id = uuid.uuid4()
        with (
            patch(
                "modulo.api.routes.pipeline_folders.update_folder",
                side_effect=ValueError("A folder cannot be its own parent"),
            ),
            patch("modulo.api.routes.pipeline_folders.set_rls_org"),
            patch("modulo.api.routes.pipeline_folders.set_rls_user_context"),
        ):
            resp = client.patch(f"/api/v1/pipeline-folders/{folder_id}", json={"parent_id": str(folder_id)})

        assert resp.status_code == 422

    def test_update_folder_cycle_returns_422(self, client: TestClient) -> None:
        folder_id = uuid.uuid4()
        with (
            patch(
                "modulo.api.routes.pipeline_folders.update_folder",
                side_effect=ValueError("Setting this parent would create a folder ancestry cycle"),
            ),
            patch("modulo.api.routes.pipeline_folders.set_rls_org"),
            patch("modulo.api.routes.pipeline_folders.set_rls_user_context"),
        ):
            resp = client.patch(
                f"/api/v1/pipeline-folders/{folder_id}",
                json={"parent_id": str(uuid.uuid4())},
            )

        assert resp.status_code == 422

    def test_update_folder_not_found_returns_404(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.pipeline_folders.update_folder", return_value=None),
            patch("modulo.api.routes.pipeline_folders.set_rls_org"),
            patch("modulo.api.routes.pipeline_folders.set_rls_user_context"),
        ):
            resp = client.patch(f"/api/v1/pipeline-folders/{uuid.uuid4()}", json={"name": "x"})

        assert resp.status_code == 404

    def test_reorder_folder_returns_200(self, client: TestClient) -> None:
        folder = _make_folder()
        folder.sort_order = 3
        with (
            patch("modulo.api.routes.pipeline_folders.update_folder", return_value=folder),
            patch("modulo.api.routes.pipeline_folders.set_rls_org"),
            patch("modulo.api.routes.pipeline_folders.set_rls_user_context"),
        ):
            resp = client.patch(f"/api/v1/pipeline-folders/{folder.id}/move", json={"sort_order": 3})

        assert resp.status_code == 200
        assert resp.json()["sort_order"] == 3

    def test_delete_folder_returns_204(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.pipeline_folders.delete_folder", return_value=True),
            patch("modulo.api.routes.pipeline_folders.set_rls_org"),
            patch("modulo.api.routes.pipeline_folders.set_rls_user_context"),
        ):
            resp = client.delete(f"/api/v1/pipeline-folders/{uuid.uuid4()}")

        assert resp.status_code == 204

    def test_delete_folder_not_found_returns_404(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.pipeline_folders.delete_folder", return_value=False),
            patch("modulo.api.routes.pipeline_folders.set_rls_org"),
            patch("modulo.api.routes.pipeline_folders.set_rls_user_context"),
        ):
            resp = client.delete(f"/api/v1/pipeline-folders/{uuid.uuid4()}")

        assert resp.status_code == 404

    def test_move_pipeline_to_folder_returns_200(self, client: TestClient) -> None:
        pipeline = _make_pipeline()
        pipeline.folder_id = uuid.uuid4()
        with (
            patch("modulo.api.routes.pipelines.move_pipeline_to_folder", return_value=pipeline),
            patch("modulo.api.routes.pipelines.set_rls_org"),
            patch("modulo.api.routes.pipelines.set_rls_user_context"),
        ):
            resp = client.patch(f"/api/v1/pipelines/{_PIPELINE_ID}/folder", json={"folder_id": str(pipeline.folder_id)})

        assert resp.status_code == 200
        assert resp.json()["folder_id"] == str(pipeline.folder_id)

    def test_move_pipeline_to_missing_folder_returns_422(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.pipelines.move_pipeline_to_folder",
                side_effect=ValueError("Folder not found"),
            ),
            patch("modulo.api.routes.pipelines.set_rls_org"),
            patch("modulo.api.routes.pipelines.set_rls_user_context"),
        ):
            resp = client.patch(
                f"/api/v1/pipelines/{_PIPELINE_ID}/folder",
                json={"folder_id": str(uuid.uuid4())},
            )

        assert resp.status_code == 422

    def test_move_pipeline_to_folder_pipeline_not_found_returns_404(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.pipelines.move_pipeline_to_folder", return_value=None),
            patch("modulo.api.routes.pipelines.set_rls_org"),
            patch("modulo.api.routes.pipelines.set_rls_user_context"),
        ):
            resp = client.patch(f"/api/v1/pipelines/{_PIPELINE_ID}/folder", json={"folder_id": None})

        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Composite node validation (PRD §8.4 Graph Validation — on-save soft checks)
# ---------------------------------------------------------------------------


class TestCompositeValidation:
    """Unit coverage for composite node validation — previously only BDD."""

    def _run_check(self, template_sub_graph: object, output_validation: dict | None = None) -> list[dict]:
        from modulo.core.graph_validator import GraphValidator
        from modulo.core.graph_validator._types import ValidationResult

        validator = GraphValidator()
        result = ValidationResult()
        node_id = "composite-node-1"
        template = SimpleNamespace(id=uuid.uuid4(), sub_pipeline_graph_json=template_sub_graph)
        validator._check_composite_subgraph(template, node_id, result)
        if output_validation:
            validator._check_output_validation(node_id, output_validation, result)
        return [{"code": i.code, "severity": i.severity, "message": i.message} for i in result.issues]

    def test_empty_sub_graph_is_error(self) -> None:
        issues = self._run_check({"nodes": [], "edges": []})
        assert any(i["code"] == "COMPOSITE_SUBGRAPH_EMPTY" and i["severity"] == "error" for i in issues)

    def test_non_dict_sub_graph_is_skipped(self) -> None:
        issues = self._run_check(None)
        assert issues == []

    def test_invalid_sub_node_type_is_error(self) -> None:
        graph = {"nodes": [{"id": "a", "node_type": "not-a-real-type"}], "edges": []}
        issues = self._run_check(graph)
        assert any(i["code"] == "COMPOSITE_SUBGRAPH_INVALID_TYPE" for i in issues)

    def test_duplicate_sub_node_id_is_error(self) -> None:
        graph = {"nodes": [{"id": "a"}, {"id": "a"}], "edges": []}
        issues = self._run_check(graph)
        assert any(i["code"] == "COMPOSITE_SUBGRAPH_DUPLICATE_NODE_ID" for i in issues)

    def test_hitl_gate_on_sub_edge_is_error(self) -> None:
        graph = {
            "nodes": [{"id": "a"}, {"id": "b"}],
            "edges": [{"source": "a", "target": "b", "hitl_gate_config": {"label": "x"}}],
        }
        issues = self._run_check(graph)
        assert any(i["code"] == "COMPOSITE_SUBGRAPH_GATE_UNSUPPORTED" for i in issues)

    def test_sub_edge_bad_source_is_error(self) -> None:
        graph = {"nodes": [{"id": "a"}, {"id": "b"}], "edges": [{"source": "ghost", "target": "b"}]}
        issues = self._run_check(graph)
        assert any(i["code"] == "COMPOSITE_SUBGRAPH_EDGE_BAD_SOURCE" for i in issues)

    def test_validation_retries_out_of_range_is_error(self) -> None:
        issues = self._run_check(None, {"max_validation_retries": 9})
        assert any(i["code"] == "COMPOSITE_VALIDATION_RETRIES_RANGE" for i in issues)

    def test_invalid_eval_type_is_error(self) -> None:
        issues = self._run_check(
            None,
            {"eval_definitions": [{"type": "made_up", "failure_behaviour": "block"}]},
        )
        assert any(i["code"] == "COMPOSITE_VALIDATION_INVALID_TYPE" for i in issues)

    def test_invalid_regex_pattern_is_error(self) -> None:
        issues = self._run_check(
            None,
            {
                "eval_definitions": [
                    {"type": "regex", "config": {"field": "x", "pattern": "("}, "failure_behaviour": "block"}
                ]
            },
        )
        assert any(i["code"] == "COMPOSITE_VALIDATION_REGEX_INVALID" for i in issues)
