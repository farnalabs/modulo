"""API contract tests — validate every endpoint returns correct response shapes."""

import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from pydantic import BaseModel, ValidationError

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.api.models.problem import ProblemDetail
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings
from tests.unit.api.mock_session import configure_mock_session

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_NOW = datetime(2025, 1, 1, tzinfo=UTC)


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


def _make_mock_session() -> AsyncMock:
    session = configure_mock_session(AsyncMock())
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


def _make_mock_account() -> MagicMock:
    account = MagicMock()
    account.id = _USER_ID
    account.email = "testuser@example.com"
    account.display_name = "Test User"
    account.active = True
    account.created_at = _NOW
    account.is_system_admin = False
    account.password_hash = None
    return account


def _make_mock_pipeline(**kwargs: Any) -> MagicMock:
    p = MagicMock()
    p.id = kwargs.get("id", uuid.uuid4())
    p.organisation_id = kwargs.get("org_id", _ORG_ID)
    p.name = kwargs.get("name", "Contract Test Pipeline")
    p.description = kwargs.get("description")
    p.visibility = kwargs.get("visibility", "org")
    p.owner_team_id = kwargs.get("owner_team_id")
    p.folder_id = kwargs.get("folder_id")
    p.max_concurrent_runs = kwargs.get("max_concurrent_runs", 5)
    p.lock_wait_timeout_seconds = kwargs.get("lock_wait_timeout_seconds", 300)
    p.node_timeout_seconds = kwargs.get("node_timeout_seconds", 300)
    p.run_context_defaults = kwargs.get("run_context_defaults", {})
    p.default_autonomy_level = kwargs.get("default_autonomy_level", "manual_approval")
    p.rate_limit_config = kwargs.get("rate_limit_config")
    p.max_duration_seconds = kwargs.get("max_duration_seconds")
    p.archived_at = kwargs.get("archived_at")
    p.snapshot_count = kwargs.get("snapshot_count", 0)
    p.created_by = kwargs.get("created_by", _USER_ID)
    p.account_id = kwargs.get("account_id", kwargs.get("created_by", _USER_ID))
    p.created_at = kwargs.get("created_at", _NOW)
    p.updated_at = kwargs.get("updated_at", _NOW)
    return p


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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_all_apiroutes() -> list[APIRoute]:
    """Extract all APIRoute instances, including those nested in included routers."""
    routes: list[APIRoute] = []
    for r in app.routes:
        tn = type(r).__name__
        if tn == "APIRoute":
            routes.append(r)
        elif tn == "_IncludedRouter" and hasattr(r, "original_router"):
            sub_router = r.original_router
            if hasattr(sub_router, "routes"):
                routes.extend(sr for sr in sub_router.routes if isinstance(sr, APIRoute))
    return routes


def get_api_routes() -> list[dict]:
    return [
        {
            "path": route.path,
            "methods": sorted((route.methods or set()) - {"HEAD", "OPTIONS"}),
            "response_model": route.response_model,
        }
        for route in _get_all_apiroutes()
        if route.path.startswith("/api/")
    ]


def validate_shape(resp_data: dict, model: type[BaseModel]) -> None:
    try:
        model.model_validate(resp_data)
    except ValidationError as e:
        lines = []
        for err in e.errors():
            loc = " -> ".join(str(p) for p in err["loc"])
            lines.append(f"  {loc}: {err['msg']} (type={err['type']})")
        pytest.fail(f"Response does not match {model.__name__}:\n" + "\n".join(lines))


# ---------------------------------------------------------------------------
# 1. Response Model Coverage Report
# ---------------------------------------------------------------------------


class TestResponseModelCoverage:
    def test_coverage_report(self) -> None:
        routes = get_api_routes()
        with_rm = [r for r in routes if r["response_model"] is not None]
        missing_rm = [r for r in routes if r["response_model"] is None]

        assert with_rm, "no /api/ routes declare a response model"
        assert len(with_rm) > len(missing_rm), (
            "the majority of /api/ routes must declare a response model; "
            f"got {len(with_rm)} declared vs {len(missing_rm)} missing"
        )

    def test_response_model_is_pydantic_model(self) -> None:
        routes = get_api_routes()
        pydantic_count = 0
        non_pydantic: list[tuple[str, str, str]] = []
        for r in routes:
            rm = r["response_model"]
            if rm is None:
                continue
            try:
                is_pydantic = isinstance(rm, type) and issubclass(rm, BaseModel)
            except TypeError:
                is_pydantic = False
            if is_pydantic:
                pydantic_count += 1
            else:
                non_pydantic.append((r["methods"][0], r["path"], str(rm)))

        assert pydantic_count > 0
        assert pydantic_count >= len(non_pydantic), (
            "most response models must be pydantic models; "
            f"got {pydantic_count} pydantic vs {len(non_pydantic)} non-pydantic: "
            f"{non_pydantic[:5]}"
        )


# ---------------------------------------------------------------------------
# 2. Schema Validation Tests
# ---------------------------------------------------------------------------


class TestAuthEndpointSchemas:
    def test_login_success_schema(self, client: TestClient) -> None:
        account = _make_mock_account()
        fake_membership = MagicMock()
        fake_membership.organisation_id = _ORG_ID
        fake_membership.role = "admin"

        with (
            patch("modulo.api.routes.auth.get_account_by_email", return_value=account),
            patch("modulo.api.routes.auth.authenticate_db_user", return_value=True),
            patch("modulo.api.routes.auth.update_last_login"),
            patch(
                "modulo.api.routes.auth.list_memberships_for_account",
                return_value=[fake_membership],
            ),
            patch(
                "modulo.api.routes.auth.create_family",
                return_value=MagicMock(family_id=uuid.uuid4()),
            ),
        ):
            resp = client.post(
                "/api/v1/auth/login",
                json={"email": "testuser@example.com", "password": "testpass"},
            )

        assert resp.status_code == 200
        from modulo.api.routes.auth import LoginResponse

        validate_shape(resp.json(), LoginResponse)

    def test_login_failure_schema(self, client: TestClient) -> None:
        with patch("modulo.api.routes.auth.get_account_by_email", return_value=None):
            resp = client.post(
                "/api/v1/auth/login",
                json={"email": "nobody", "password": "wrong"},
            )

        assert resp.status_code == 401
        validate_shape(resp.json(), ProblemDetail)

    def test_me_schema(self, client: TestClient) -> None:
        account = _make_mock_account()
        with (
            patch("modulo.api.routes.auth.get_account_by_id", return_value=account),
            patch(
                "modulo.api.routes.auth.resolve_role_from_membership",
                new=AsyncMock(return_value="admin"),
            ),
        ):
            resp = client.get("/api/v1/auth/me")

        assert resp.status_code == 200
        from modulo.api.routes.auth import MeResponse

        validate_shape(resp.json(), MeResponse)

    def test_ws_token_schema(self, client: TestClient) -> None:
        # ws-token is swept via require_permission("run.status") (ADR 017), which
        # resolves the authz-enforce kill switch through the DI session. Provide a
        # session whose execute returns a scalar (enforce=True) plus a tenant
        # principal override so the test does not touch a real database.
        from modulo.auth.dependencies import get_current_tenant_user
        from modulo.auth.jwt import TenantPrincipal

        mock_session = configure_mock_session(AsyncMock())
        begin_cm = MagicMock()
        begin_cm.__aenter__ = AsyncMock(return_value=None)
        begin_cm.__aexit__ = AsyncMock(return_value=False)
        mock_session.begin = MagicMock(return_value=begin_cm)
        result = MagicMock()
        result.scalar_one_or_none.return_value = True
        mock_session.execute = AsyncMock(return_value=result)

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield mock_session

        app.dependency_overrides[get_db_session] = override_session
        app.dependency_overrides[get_current_tenant_user] = lambda: TenantPrincipal(
            username="testuser", organisation_id=_ORG_ID, account_id=_USER_ID, org_role="admin"
        )
        with (
            patch("redis.asyncio.Redis.from_url") as mock_redis_factory,
            patch(
                "modulo.api.routes.auth.resolve_role_from_membership",
                new=AsyncMock(return_value="admin"),
            ),
        ):
            mock_redis = AsyncMock()
            mock_redis.setex = AsyncMock()
            mock_redis.aclose = AsyncMock()
            mock_redis_factory.return_value = mock_redis
            resp = client.post("/api/v1/auth/ws-token")
        assert resp.status_code == 200
        from modulo.api.routes.auth import WsTokenResponse

        validate_shape(resp.json(), WsTokenResponse)

    def test_me_without_token_returns_error(self) -> None:
        unauth = TestClient(app)
        resp = unauth.get("/api/v1/auth/me")
        assert resp.status_code in (401, 403)


class TestPipelineEndpointSchemas:
    def test_list_pipelines_schema(self, client: TestClient) -> None:
        page_result = MagicMock()
        page_result.items = []
        page_result.total = 0
        page_result.page = 1
        page_result.page_size = 20
        page_result.next_cursor = None

        with (
            patch("modulo.api.routes.pipelines.list_pipelines", return_value=page_result),
            patch("modulo.api.routes.pipelines.set_rls_org"),
            patch("modulo.api.routes.pipelines.set_rls_user_context"),
        ):
            resp = client.get("/api/v1/pipelines")

        assert resp.status_code == 200
        from modulo.api.routes.pipelines import PipelineListResponse

        validate_shape(resp.json(), PipelineListResponse)

    def test_create_pipeline_schema(self, client: TestClient) -> None:
        pipeline = _make_mock_pipeline()

        with (
            patch("modulo.api.routes.pipelines.create_pipeline", return_value=pipeline),
            patch("modulo.api.routes.pipelines.set_rls_org"),
            patch("modulo.api.routes.pipelines.set_rls_user_context"),
        ):
            resp = client.post("/api/v1/pipelines", json={"name": "Test"})

        assert resp.status_code == 201
        from modulo.api.routes.pipelines import PipelineResponse

        validate_shape(resp.json(), PipelineResponse)

    def test_get_pipeline_schema(self, client: TestClient) -> None:
        pipeline = _make_mock_pipeline()

        with (
            patch("modulo.api.routes.pipelines.get_pipeline", return_value=pipeline),
            patch("modulo.api.routes.pipelines.set_rls_org"),
            patch("modulo.api.routes.pipelines.set_rls_user_context"),
        ):
            resp = client.get(f"/api/v1/pipelines/{pipeline.id}")

        assert resp.status_code == 200
        from modulo.api.routes.pipelines import PipelineResponse

        validate_shape(resp.json(), PipelineResponse)

    def test_get_pipeline_404_schema(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.pipelines.get_pipeline", return_value=None),
            patch("modulo.api.routes.pipelines.set_rls_org"),
            patch("modulo.api.routes.pipelines.set_rls_user_context"),
        ):
            resp = client.get(f"/api/v1/pipelines/{uuid.uuid4()}")

        assert resp.status_code == 404
        validate_shape(resp.json(), ProblemDetail)

    def test_update_pipeline_schema(self, client: TestClient) -> None:
        pipeline = _make_mock_pipeline()

        with (
            patch("modulo.api.routes.pipelines.get_pipeline", return_value=pipeline),
            patch("modulo.api.routes.pipelines.update_pipeline", return_value=pipeline),
            patch("modulo.api.routes.pipelines.set_rls_org"),
            patch("modulo.api.routes.pipelines.set_rls_user_context"),
        ):
            resp = client.patch(f"/api/v1/pipelines/{pipeline.id}", json={"name": "Updated"})

        assert resp.status_code == 200
        from modulo.api.routes.pipelines import PipelineResponse

        validate_shape(resp.json(), PipelineResponse)

    def test_get_pipeline_graph_404_schema(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.pipelines.get_pipeline_graph", return_value=None),
            patch("modulo.api.routes.pipelines.set_rls_org"),
            patch("modulo.api.routes.pipelines.set_rls_user_context"),
        ):
            resp = client.get(f"/api/v1/pipelines/{uuid.uuid4()}/graph")

        assert resp.status_code == 404
        validate_shape(resp.json(), ProblemDetail)


class TestApiKeyEndpointSchemas:
    def test_create_api_key_schema(self, client: TestClient) -> None:
        key_mock = MagicMock()
        key_mock.id = uuid.uuid4()
        key_mock.name = "Test Key"
        key_mock.role = "operator"
        key_mock.lookup_prefix = "abc123"
        key_mock.created_at = _NOW

        with (
            patch(
                "modulo.api.routes.api_keys.create_api_key",
                return_value=(key_mock, "mk_test_full_key"),
            ),
            patch("modulo.api.routes.api_keys.set_rls_org"),
            patch(
                "modulo.api.routes.api_keys.resolve_role_from_membership",
                new=AsyncMock(return_value="admin"),
            ),
        ):
            resp = client.post("/api/v1/api-keys", json={"name": "Test Key"})

        assert resp.status_code == 201
        from modulo.api.routes.api_keys import ApiKeyCreatedResponse

        validate_shape(resp.json(), ApiKeyCreatedResponse)

    def test_mcp_config_schema(self, client: TestClient) -> None:
        resp = client.get("/api/v1/api-keys/mcp-config")
        assert resp.status_code == 200
        from modulo.api.routes.api_keys import McpConfigResponse

        validate_shape(resp.json(), McpConfigResponse)

    def test_revoke_api_key_schema(self, client: TestClient) -> None:
        key_id = uuid.uuid4()

        with (
            patch("modulo.api.routes.api_keys.revoke_api_key", return_value=True),
            patch("modulo.api.routes.api_keys.set_rls_org"),
        ):
            resp = client.delete(f"/api/v1/api-keys/{key_id}")

        assert resp.status_code == 200
        from modulo.api.routes.api_keys import ApiKeyRevokeResponse

        validate_shape(resp.json(), ApiKeyRevokeResponse)


class TestPipelineSnapshotSchemas:
    def test_list_snapshots_schema(self, client: TestClient) -> None:
        pipeline_id = uuid.uuid4()

        with (
            patch("modulo.api.routes.pipelines.get_pipeline", return_value=_make_mock_pipeline(id=pipeline_id)),
            patch("modulo.api.routes.pipelines.list_snapshots", new=AsyncMock(return_value=([], 0))),
            patch("modulo.api.routes.pipelines.set_rls_org"),
            patch("modulo.api.routes.pipelines.set_rls_user_context"),
        ):
            resp = client.get(f"/api/v1/pipelines/{pipeline_id}/snapshots")

        assert resp.status_code == 200
        from modulo.api.routes.pipelines import SnapshotListResponse

        validate_shape(resp.json(), SnapshotListResponse)

    def test_get_snapshot_404_schema(self, client: TestClient) -> None:
        pipeline_id = uuid.uuid4()
        snapshot_id = uuid.uuid4()

        with (
            patch("modulo.api.routes.pipelines.get_snapshot_detail", new=AsyncMock(return_value=None)),
            patch("modulo.api.routes.pipelines.set_rls_org"),
            patch("modulo.api.routes.pipelines.set_rls_user_context"),
        ):
            resp = client.get(f"/api/v1/pipelines/{pipeline_id}/snapshots/{snapshot_id}")

        assert resp.status_code == 404
        validate_shape(resp.json(), ProblemDetail)


class TestConnectorEndpointSchemas:
    def test_list_connectors_schema(self, client: TestClient) -> None:
        page = MagicMock()
        page.items = []
        page.total = 0
        page.page = 1
        page.page_size = 20
        page.next_cursor = None

        with (
            patch("modulo.api.routes.connectors.list_connector_instances", return_value=page),
            patch("modulo.api.routes.connectors.set_rls_org"),
            patch("modulo.api.routes.connectors.set_rls_user_context"),
        ):
            resp = client.get("/api/v1/connectors")

        assert resp.status_code == 200
        from modulo.api.routes.connectors import ConnectorListResponse

        validate_shape(resp.json(), ConnectorListResponse)

    def test_create_connector_schema(self, client: TestClient) -> None:
        connector = MagicMock()
        connector.id = uuid.uuid4()
        connector.organisation_id = _ORG_ID
        connector.name = "Test"
        connector.connector_type_id = "filesystem"
        connector.credentials_ciphertext = b""
        connector.config_json = {}
        connector.allowed_operations = []
        connector.status = "connected"
        connector.visibility = "org"
        connector.owner_team_id = None
        connector.tier = "native"
        connector.created_at = _NOW
        connector.updated_at = _NOW
        connector.description = None
        connector.degraded_at = None
        connector.last_skip_error = None

        with (
            patch("modulo.api.routes.connectors.create_connector_instance", return_value=connector),
            patch("modulo.api.routes.connectors._encrypt", return_value=b"encrypted"),
            patch("modulo.api.routes.connectors.set_rls_org"),
            patch("modulo.api.routes.connectors.set_rls_user_context"),
        ):
            resp = client.post(
                "/api/v1/connectors",
                json={
                    "name": "Test",
                    "connector_type_id": "filesystem",
                    "credentials": "test-credentials",
                    "config_json": {},
                    "allowed_operations": [],
                },
            )

        assert resp.status_code == 201
        from modulo.api.routes.connectors import ConnectorResponse

        validate_shape(resp.json(), ConnectorResponse)


class TestSchemaEndpointSchemas:
    def test_list_schemas_schema(self, client: TestClient) -> None:
        page = MagicMock()
        page.items = []
        page.total = 0
        page.page = 1
        page.page_size = 20

        with (
            patch("modulo.api.routes.schemas.list_schemas", return_value=page),
            patch("modulo.api.routes.schemas.set_rls_org"),
        ):
            resp = client.get("/api/v1/schemas")

        assert resp.status_code == 200
        from modulo.api.routes.schemas import SchemaListResponse

        validate_shape(resp.json(), SchemaListResponse)


class TestModelBackendEndpointSchemas:
    def test_list_model_backends_schema(self, client: TestClient) -> None:
        page = MagicMock()
        page.items = []
        page.total = 0
        page.page = 1
        page.page_size = 20

        with (
            patch("modulo.api.routes.model_backends.list_model_backends", return_value=page),
            patch("modulo.api.routes.model_backends.set_rls_org"),
            patch("modulo.api.routes.model_backends.set_rls_user_context"),
        ):
            resp = client.get("/api/v1/model-backends")

        assert resp.status_code == 200
        from modulo.api.routes.model_backends import ModelBackendListResponse

        validate_shape(resp.json(), ModelBackendListResponse)


# ---------------------------------------------------------------------------
# 3. Error Response Shape Tests
# ---------------------------------------------------------------------------


class TestErrorResponseShapes:
    def test_404_not_found(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.pipelines.get_pipeline", return_value=None),
            patch("modulo.api.routes.pipelines.set_rls_org"),
            patch("modulo.api.routes.pipelines.set_rls_user_context"),
        ):
            resp = client.get(f"/api/v1/pipelines/{uuid.uuid4()}")

        assert resp.status_code == 404
        validate_shape(resp.json(), ProblemDetail)

    def test_422_validation_error(self, client: TestClient) -> None:
        resp = client.post("/api/v1/pipelines", json={})
        assert resp.status_code == 422
        validate_shape(resp.json(), ProblemDetail)

    def test_401_unauthorized(self) -> None:
        unauth = TestClient(app)
        resp = unauth.get("/api/v1/pipelines")
        assert resp.status_code == 401
        validate_shape(resp.json(), ProblemDetail)

    def test_403_forbidden(self, client: TestClient) -> None:
        viewer = AuthenticatedPrincipal(
            username="viewer",
            organisation_id=_ORG_ID,
            account_id=_USER_ID,
            org_role="viewer",
        )
        original_override = app.dependency_overrides.get(get_current_user)
        app.dependency_overrides[get_current_user] = lambda: viewer

        with patch("modulo.api.routes.admin.set_rls_org"):
            resp = client.get("/api/v1/admin/search?q=test")

        if original_override is not None:
            app.dependency_overrides[get_current_user] = original_override
        else:
            del app.dependency_overrides[get_current_user]

        assert resp.status_code == 403
        validate_shape(resp.json(), ProblemDetail)
