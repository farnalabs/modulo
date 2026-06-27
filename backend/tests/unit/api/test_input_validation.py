"""Tests for input validation constraints across all route Pydantic models.

Validates that:
- String fields enforce min/max_length
- Numeric fields enforce ge/le
- Pattern-constrained fields reject invalid values
- Routes that accept raw body are properly typed

These tests validate Pydantic model definitions directly (fast, no DB) and
route-level validation via TestClient (ensures FastAPI rejects bad input).
"""

import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
        modulo_license_key="test-license-key",
    )


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
# Pipeline create validation
# ---------------------------------------------------------------------------


def test_pipeline_create_empty_name(client: TestClient) -> None:
    resp = client.post("/api/v1/pipelines", json={"name": ""})
    assert resp.status_code == 422


def test_pipeline_create_name_too_long(client: TestClient) -> None:
    resp = client.post("/api/v1/pipelines", json={"name": "x" * 256})
    assert resp.status_code == 422


def test_pipeline_create_invalid_visibility(client: TestClient) -> None:
    resp = client.post("/api/v1/pipelines", json={"name": "Test", "visibility": "public"})
    assert resp.status_code == 422


def test_pipeline_create_valid_minimal(client: TestClient) -> None:
    pipeline = MagicMock()
    pipeline.id = uuid.uuid4()
    pipeline.organisation_id = _ORG_ID
    pipeline.name = "Test"
    pipeline.description = None
    pipeline.visibility = "org"
    pipeline.max_concurrent_runs = 5
    pipeline.lock_wait_timeout_seconds = 300
    pipeline.node_timeout_seconds = 300
    pipeline.run_context_defaults = {}
    pipeline.default_autonomy_level = "manual_approval"
    pipeline.created_by = uuid.uuid4()
    pipeline.created_at = datetime(2025, 1, 1, tzinfo=UTC)
    pipeline.updated_at = datetime(2025, 1, 1, tzinfo=UTC)

    with (
        patch("modulo.api.routes.pipelines.create_pipeline", return_value=pipeline),
        patch("modulo.api.routes.pipelines.set_rls_org"),
    ):
        resp = client.post("/api/v1/pipelines", json={"name": "Test"})
    assert resp.status_code == 201


# ---------------------------------------------------------------------------
# Pipeline pagination bounds
# ---------------------------------------------------------------------------


def test_pipeline_list_page_zero(client: TestClient) -> None:
    resp = client.get("/api/v1/pipelines?page=0")
    assert resp.status_code == 422


def test_pipeline_list_page_size_over_limit(client: TestClient) -> None:
    resp = client.get("/api/v1/pipelines?page_size=200")
    assert resp.status_code == 422


def test_pipeline_list_negative_page(client: TestClient) -> None:
    resp = client.get("/api/v1/pipelines?page=-1")
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Schema create validation
# ---------------------------------------------------------------------------


def test_schema_create_empty_name(client: TestClient) -> None:
    with patch("modulo.api.routes.schemas.set_rls_org"):
        resp = client.post("/api/v1/schemas", json={"name": ""})
    assert resp.status_code == 422


def test_schema_create_name_too_long(client: TestClient) -> None:
    with patch("modulo.api.routes.schemas.set_rls_org"):
        resp = client.post("/api/v1/schemas", json={"name": "x" * 256})
    assert resp.status_code == 422


def test_schema_create_valid(client: TestClient) -> None:
    mock_schema = MagicMock()
    mock_schema.id = uuid.uuid4()
    mock_schema.organisation_id = _ORG_ID
    mock_schema.name = "Test"
    mock_schema.description = None
    mock_schema.abstract_name = None
    mock_schema.created_by = uuid.uuid4()
    mock_schema.created_at = datetime(2025, 1, 1, tzinfo=UTC)
    mock_schema.updated_at = datetime(2025, 1, 1, tzinfo=UTC)

    with (
        patch("modulo.api.routes.schemas.create_schema", return_value=mock_schema),
        patch("modulo.api.routes.schemas.set_rls_org"),
    ):
        resp = client.post("/api/v1/schemas", json={"name": "Test"})
    assert resp.status_code == 201


# ---------------------------------------------------------------------------
# Eval definition validation
# ---------------------------------------------------------------------------


def test_eval_create_invalid_eval_type(client: TestClient) -> None:
    with patch("modulo.api.routes.evals.set_rls_org"):
        resp = client.post(
            "/api/v1/evals",
            json={
                "pipeline_id": str(uuid.uuid4()),
                "name": "Test Eval",
                "eval_type": "invalid_type",
            },
        )
    assert resp.status_code == 422


def test_eval_create_empty_name(client: TestClient) -> None:
    with patch("modulo.api.routes.evals.set_rls_org"):
        resp = client.post(
            "/api/v1/evals",
            json={
                "pipeline_id": str(uuid.uuid4()),
                "name": "",
                "eval_type": "llm_judge",
            },
        )
    assert resp.status_code == 422


def test_eval_create_valid_eval_types(client: TestClient) -> None:
    for eval_type in ("llm_judge", "regex", "json_schema", "custom_function"):
        with patch("modulo.api.routes.evals.set_rls_org"):
            resp = client.post(
                "/api/v1/evals",
                json={
                    "pipeline_id": str(uuid.uuid4()),
                    "name": "Test Eval",
                    "eval_type": eval_type,
                },
            )
        # Should pass Pydantic validation (not 422). May fail handler
        # with 403 (admin check) but that's fine -- test is about validation.
        assert resp.status_code != 422, f"eval_type={eval_type} got 422: {resp.json()}"


# ---------------------------------------------------------------------------
# Audit query params length bounds
# ---------------------------------------------------------------------------


def test_audit_list_long_event_type(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.audit.list_audit_events"),
        patch("modulo.api.routes.audit.set_rls_org"),
    ):
        resp = client.get("/api/v1/admin/audit?event_type=" + "x" * 100)
    assert resp.status_code == 422


def test_audit_list_long_cursor(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.audit.list_audit_events"),
        patch("modulo.api.routes.audit.set_rls_org"),
    ):
        resp = client.get("/api/v1/admin/audit?cursor=" + "x" * 100)
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Run stats validation
# ---------------------------------------------------------------------------


def test_run_stats_invalid_period(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.runs.get_run_stats"),
        patch("modulo.api.routes.runs.set_rls_org"),
    ):
        resp = client.get("/api/v1/runs/stats?period=invalid")
    assert resp.status_code == 422


def test_run_heatmap_invalid_year(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.runs.get_run_heatmap"),
        patch("modulo.api.routes.runs.set_rls_org"),
    ):
        resp = client.get("/api/v1/runs/stats/heatmap?year=1999")
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Library import analyse — was using raw dict body
# ---------------------------------------------------------------------------


def test_analyse_import_bundle_missing_bundle_key(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.library._analyse_bundle"),
        patch("modulo.api.routes.library.set_rls_org"),
    ):
        resp = client.post("/api/v1/libraries/import/analyse", json={})
    assert resp.status_code == 422


def test_analyse_import_bundle_valid_shape(client: TestClient) -> None:
    """Valid body shape should not get 422 (may fail at handler level)."""
    bundle = {"pipeline": {"name": "Test"}}
    with patch("modulo.api.routes.library.set_rls_org"):
        resp = client.post("/api/v1/libraries/import/analyse", json={"bundle": bundle})
    assert resp.status_code != 422


# ---------------------------------------------------------------------------
# Pipeline graph — nodes & edges bounds
# ---------------------------------------------------------------------------


def test_pipeline_graph_too_many_nodes(client: TestClient) -> None:
    nodes = [
        {"id": str(uuid.uuid4()), "node_type": "agent", "agent_id": str(uuid.uuid4()), "position": {"x": 0, "y": 0}}
        for _ in range(501)
    ]
    resp = client.patch(
        f"/api/v1/pipelines/{uuid.uuid4()}/graph",
        json={
            "nodes": nodes,
            "edges": [],
        },
    )
    assert resp.status_code == 422


def test_pipeline_graph_duplicate_node_ids(client: TestClient) -> None:
    dup_id = str(uuid.uuid4())
    nodes = [
        {"id": dup_id, "node_type": "agent", "agent_id": str(uuid.uuid4()), "position": {"x": 0, "y": 0}},
        {"id": dup_id, "node_type": "agent", "agent_id": str(uuid.uuid4()), "position": {"x": 100, "y": 0}},
    ]
    resp = client.patch(
        f"/api/v1/pipelines/{uuid.uuid4()}/graph",
        json={
            "nodes": nodes,
            "edges": [],
        },
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Auth — login empty fields
# ---------------------------------------------------------------------------


def test_login_empty_email(client: TestClient) -> None:
    resp = client.post("/api/v1/auth/login", json={"email": "", "password": "secret"})
    assert resp.status_code == 422


def test_login_empty_password(client: TestClient) -> None:
    resp = client.post("/api/v1/auth/login", json={"email": "a@b.com", "password": ""})
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Team name bounds
# ---------------------------------------------------------------------------


def test_create_team_empty_name(client: TestClient) -> None:
    with patch("modulo.api.routes.teams.set_rls_org"):
        resp = client.post("/api/v1/teams", json={"name": ""})
    assert resp.status_code == 422


def test_create_team_name_too_long(client: TestClient) -> None:
    with patch("modulo.api.routes.teams.set_rls_org"):
        resp = client.post("/api/v1/teams", json={"name": "x" * 256})
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Connector name bounds
# ---------------------------------------------------------------------------


def test_create_connector_empty_name(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.connectors.set_rls_org"),
        patch("modulo.api.routes.connectors.create_connector_instance"),
    ):
        resp = client.post(
            "/api/v1/connectors",
            json={
                "name": "",
                "connector_type_id": "github",
                "credentials": "tok",
            },
        )
    assert resp.status_code == 422


def test_create_connector_name_too_long(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.connectors.set_rls_org"),
        patch("modulo.api.routes.connectors.create_connector_instance"),
    ):
        resp = client.post(
            "/api/v1/connectors",
            json={
                "name": "x" * 256,
                "connector_type_id": "github",
                "credentials": "tok",
            },
        )
    assert resp.status_code == 422


def test_create_connector_type_id_too_long(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.connectors.set_rls_org"),
        patch("modulo.api.routes.connectors.create_connector_instance"),
    ):
        resp = client.post(
            "/api/v1/connectors",
            json={
                "name": "Test",
                "connector_type_id": "x" * 200,
                "credentials": "tok",
            },
        )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Model backend name bounds
# ---------------------------------------------------------------------------


def test_create_model_backend_empty_name(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.model_backends.set_rls_org"),
        patch("modulo.api.routes.model_backends.create_model_backend"),
    ):
        resp = client.post(
            "/api/v1/model-backends",
            json={
                "name": "",
                "display_name": "D",
                "provider": "openai",
                "model_id": "gpt-4",
                "api_key": "sk-test",
            },
        )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Library primitive pattern validation
# ---------------------------------------------------------------------------


def test_create_library_primitive_invalid_type(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.library.set_rls_org"),
        patch("modulo.api.routes.library.create_library_primitive"),
    ):
        resp = client.post(
            "/api/v1/libraries",
            json={
                "primitive_type": "invalid",
                "name": "Test",
                "slug": "test",
                "content_json": {},
            },
        )
    assert resp.status_code == 422


def test_create_library_primitive_empty_name(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.library.set_rls_org"),
        patch("modulo.api.routes.library.create_library_primitive"),
    ):
        resp = client.post(
            "/api/v1/libraries",
            json={
                "primitive_type": "schema",
                "name": "",
                "slug": "test",
                "content_json": {},
            },
        )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Environment profile bounds
# ---------------------------------------------------------------------------


def test_create_environment_profile_empty_name(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.environments.set_rls_org"),
        patch("modulo.api.routes.environments.create_environment_profile"),
    ):
        resp = client.post(
            "/api/v1/environments",
            json={
                "name": "",
                "image_ref": "img",
            },
        )
    assert resp.status_code == 422


def test_create_environment_profile_timeout_too_low(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.environments.set_rls_org"),
        patch("modulo.api.routes.environments.create_environment_profile"),
    ):
        resp = client.post(
            "/api/v1/environments",
            json={
                "name": "Test",
                "image_ref": "img",
                "timeout_seconds": 10,
            },
        )
    assert resp.status_code == 422


def test_create_environment_profile_timeout_too_high(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.environments.set_rls_org"),
        patch("modulo.api.routes.environments.create_environment_profile"),
    ):
        resp = client.post(
            "/api/v1/environments",
            json={
                "name": "Test",
                "image_ref": "img",
                "timeout_seconds": 90000,
            },
        )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# HITL claim expiry bounds
# ---------------------------------------------------------------------------


def test_hitl_claim_expiry_too_low(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.hitl.set_rls_org"),
        patch("modulo.api.routes.hitl.HITLManager"),
        patch("modulo.api.routes.hitl.update_run_status"),
    ):
        resp = client.post(
            f"/api/v1/runs/{uuid.uuid4()}/hitl/{uuid.uuid4()}/claim",
            json={"expiry_minutes": 0},
        )
    assert resp.status_code == 422


def test_hitl_claim_expiry_too_high(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.hitl.set_rls_org"),
        patch("modulo.api.routes.hitl.HITLManager"),
        patch("modulo.api.routes.hitl.update_run_status"),
    ):
        resp = client.post(
            f"/api/v1/runs/{uuid.uuid4()}/hitl/{uuid.uuid4()}/claim",
            json={"expiry_minutes": 1500},
        )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Registry publish validation
# ---------------------------------------------------------------------------


def test_registry_publish_empty_author(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/registry/primitives",
        json={
            "author": "",
            "name": "test",
            "primitive_type": "schema",
            "content_json": {},
            "signing_key_hex": "x" * 64,
        },
    )
    assert resp.status_code == 422


def test_registry_publish_invalid_primitive_type(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/registry/primitives",
        json={
            "author": "author",
            "name": "test",
            "primitive_type": "bad",
            "content_json": {},
            "signing_key_hex": "x" * 64,
        },
    )
    assert resp.status_code == 422


def test_registry_register_publisher_empty_author(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/registry/publishers",
        json={
            "fingerprint_hex": "x" * 32,
            "author": "",
            "name": "pub",
        },
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Agent create validation
# ---------------------------------------------------------------------------


def test_agent_create_empty_name(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.agents.set_rls_org"),
        patch("modulo.api.routes.agents.create_agent"),
    ):
        resp = client.post(
            "/api/v1/agents",
            json={
                "name": "",
                "input_schema_id": str(uuid.uuid4()),
                "input_schema_version": "1.0",
                "output_schema_id": str(uuid.uuid4()),
                "output_schema_version": "1.0",
                "prompt_template": "You are an agent",
                "model_backend_id": str(uuid.uuid4()),
            },
        )
    assert resp.status_code == 422


def test_agent_create_empty_prompt(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.agents.set_rls_org"),
        patch("modulo.api.routes.agents.create_agent"),
    ):
        resp = client.post(
            "/api/v1/agents",
            json={
                "name": "Agent",
                "input_schema_id": str(uuid.uuid4()),
                "input_schema_version": "1.0",
                "output_schema_id": str(uuid.uuid4()),
                "output_schema_version": "1.0",
                "prompt_template": "",
                "model_backend_id": str(uuid.uuid4()),
            },
        )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Contribution validation
# ---------------------------------------------------------------------------


def test_contribute_empty_name(client: TestClient) -> None:
    with (
        patch("modulo.api.routes.contributions.set_rls_org"),
        patch("modulo.api.routes.contributions.contribute_fixture"),
    ):
        resp = client.post(
            "/api/v1/library/contribute",
            json={
                "name": "",
                "slug": "test",
                "fixture_map": {},
            },
        )
    assert resp.status_code == 422
