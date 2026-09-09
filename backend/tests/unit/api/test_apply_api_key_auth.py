"""Real-dependency-stack API-key (``mk_``) auth tests for the endpoints the
``modulo apply`` CLI uses (FAR-681 slice 1).

Unlike the per-route fixtures in test_schemas_endpoint.py /
test_model_backends_endpoint.py (which override ``get_current_user``), these
tests exercise the REAL ``require_permission_any_credential`` dependency chain —
including ``get_current_tenant_user_or_api_key``'s mk_ key resolution — so an
``Authorization: Bearer mk_...`` header genuinely authenticates and creates a
schema + a model backend.
"""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.settings import Settings, get_settings
from tests.unit.api.mock_session import configure_mock_session

_FERNET_KEY = Fernet.generate_key().decode()
_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_NOW_STAMP = "2025-01-01T00:00:00Z"

_KEY = "mk_12345678_" + "x" * 32

_SCHEMA_CREATE_BODY = {"name": "Test Schema"}
_BACKEND_CREATE_BODY = {
    "name": "Test Backend",
    "display_name": "GPT-4",
    "provider": "openai",
    "model_id": "gpt-4",
    "api_key": "sk-test",
    "default_params": {"temperature": 0.5},
}
_AUTH_HEADERS = {"Authorization": f"Bearer {_KEY}"}


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_FERNET_KEY,
        modulo_admin_password="testpass",
    )


def _fake_key(role: str = "operator") -> SimpleNamespace:
    return SimpleNamespace(
        name="apply-key",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        role=role,
    )


def _make_auth_session() -> AsyncMock:
    """Session for get_current_tenant_user_or_api_key's internal org lookups."""
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    session.in_transaction = MagicMock(return_value=True)
    session.get_bind = MagicMock()
    session.get_bind.return_value.dialect.name = "sqlite"
    result = MagicMock()
    result.scalar_one_or_none.return_value = _fake_key()
    session.execute = AsyncMock(return_value=result)
    return session


class _FakeFactory:
    """async_sessionmaker stand-in for the API-key auth path."""

    def __init__(self, session: AsyncMock) -> None:
        self._session = session

    def __call__(self):
        return self

    async def __aenter__(self) -> AsyncMock:
        return self._session

    async def __aexit__(self, *args: object) -> None:
        return False


def _make_route_session() -> AsyncMock:
    """Route session: no duplicate rows, generic reads permitted."""
    session = AsyncMock()
    configure_mock_session(session)
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    default_result = MagicMock()
    default_result.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(return_value=default_result)
    return session


class TestApplyApiKeyAuth:
    def test_api_key_creates_schema(self) -> None:
        schema = MagicMock()
        schema.id = uuid.uuid4()
        schema.organisation_id = _ORG_ID
        schema.name = "Test Schema"
        schema.description = None
        schema.abstract_name = None
        schema.folder_id = None
        schema.account_id = _USER_ID
        schema.created_at = _NOW_STAMP
        schema.updated_at = _NOW_STAMP

        route_session = _make_route_session()
        auth_session = _make_auth_session()

        def override_session():
            yield route_session

        app.dependency_overrides[get_settings] = _make_settings
        app.dependency_overrides[get_db_session] = override_session
        app.dependency_overrides[_get_engine] = lambda: MagicMock()
        mock_plan = MagicMock()
        mock_plan.feature_enabled.return_value = True
        app.dependency_overrides[get_plan_context] = lambda: mock_plan
        try:
            with (
                patch("modulo.api.dependencies.get_or_create_engine", return_value=MagicMock()),
                patch(
                    "modulo.api.dependencies.get_or_create_session_factory",
                    return_value=_FakeFactory(auth_session),
                ),
                patch("modulo.auth.api_key.validate_api_key", return_value=_fake_key("operator")),
                patch(
                    "modulo.auth.dependencies.resolve_role_from_membership",
                    new=AsyncMock(return_value="operator"),
                ),
                patch("modulo.api.routes.schemas.create_schema", return_value=schema),
            ):
                client = TestClient(app)
                resp = client.post("/api/v1/schemas", json=_SCHEMA_CREATE_BODY, headers=_AUTH_HEADERS)
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 201, resp.text
        assert resp.json()["name"] == "Test Schema"

    def test_api_key_creates_model_backend(self) -> None:
        backend = MagicMock()
        backend.id = uuid.uuid4()
        backend.organisation_id = _ORG_ID
        backend.name = "Test Backend"
        backend.display_name = "GPT-4"
        backend.provider = "openai"
        backend.model_id = "gpt-4"
        backend.credentials_ciphertext = b"ciphertext"
        backend.default_params = {"temperature": 0.5}
        backend.visibility = "org"
        backend.owner_team_id = None
        backend.tier = "native"
        backend.fallback_backend_ids = None
        backend.account_id = _USER_ID
        backend.created_at = _NOW_STAMP
        backend.updated_at = _NOW_STAMP

        route_session = _make_route_session()
        auth_session = _make_auth_session()

        def override_session():
            yield route_session

        app.dependency_overrides[get_settings] = _make_settings
        app.dependency_overrides[get_db_session] = override_session
        app.dependency_overrides[_get_engine] = lambda: MagicMock()
        mock_plan = MagicMock()
        mock_plan.feature_enabled.return_value = True
        app.dependency_overrides[get_plan_context] = lambda: mock_plan
        try:
            with (
                patch("modulo.api.dependencies.get_or_create_engine", return_value=MagicMock()),
                patch(
                    "modulo.api.dependencies.get_or_create_session_factory",
                    return_value=_FakeFactory(auth_session),
                ),
                patch("modulo.auth.api_key.validate_api_key", return_value=_fake_key("operator")),
                patch(
                    "modulo.auth.dependencies.resolve_role_from_membership",
                    new=AsyncMock(return_value="operator"),
                ),
                patch("modulo.api.routes.model_backends.get_model_backend", return_value=backend),
                patch("modulo.api.routes.model_backends.create_model_backend", return_value=backend),
                patch("modulo.api.routes.model_backends.create_secrets_backend", return_value=AsyncMock()),
                patch(
                    "modulo.api.routes.model_backends._run_health_check_on_save",
                    new=AsyncMock(return_value=("ok", None)),
                ),
            ):
                client = TestClient(app)
                resp = client.post("/api/v1/model-backends", json=_BACKEND_CREATE_BODY, headers=_AUTH_HEADERS)
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 201, resp.text
        # default_params survived the write round-trip (the response echoes
        # the entity's stored default_params).
        assert resp.json()["default_params"] == {"temperature": 0.5}

    def test_api_key_caller_with_insufficient_role_is_denied(self) -> None:
        """A runner-role key gets 403 on the operator-gated schema create."""
        route_session = _make_route_session()
        auth_session = _make_auth_session()

        def override_session():
            yield route_session

        app.dependency_overrides[get_settings] = _make_settings
        app.dependency_overrides[get_db_session] = override_session
        app.dependency_overrides[_get_engine] = lambda: MagicMock()
        mock_plan = MagicMock()
        mock_plan.feature_enabled.return_value = True
        app.dependency_overrides[get_plan_context] = lambda: mock_plan
        try:
            with (
                patch("modulo.api.dependencies.get_or_create_engine", return_value=MagicMock()),
                patch(
                    "modulo.api.dependencies.get_or_create_session_factory",
                    return_value=_FakeFactory(auth_session),
                ),
                patch("modulo.auth.api_key.validate_api_key", return_value=_fake_key("runner")),
                patch(
                    "modulo.auth.dependencies.resolve_role_from_membership",
                    new=AsyncMock(return_value="runner"),
                ),
            ):
                client = TestClient(app)
                resp = client.post("/api/v1/schemas", json=_SCHEMA_CREATE_BODY, headers=_AUTH_HEADERS)
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 403

    def test_api_key_creates_pipeline(self) -> None:
        """The document create endpoint accepts the mk_ API-key bearer through
        the REAL require_permission_any_credential stack (FAR-681 slice 2)."""
        pipeline = MagicMock()
        pipeline.id = uuid.uuid4()
        pipeline.organisation_id = _ORG_ID
        pipeline.name = "Apply Pipeline"
        pipeline.description = None
        pipeline.visibility = "org"
        pipeline.owner_team_id = None
        pipeline.max_concurrent_runs = 5
        pipeline.lock_wait_timeout_seconds = 300
        pipeline.node_timeout_seconds = 300
        pipeline.run_context_defaults = {}
        pipeline.default_autonomy_level = "manual_approval"
        pipeline.rate_limit_config = None
        pipeline.retry_policy = {}
        pipeline.max_duration_seconds = None
        pipeline.stale_run_timeout_minutes = 30
        pipeline.snapshot_count = 0
        pipeline.graph_nodes_json = []
        pipeline.connector_rebind_required = False
        pipeline.archived_at = None
        pipeline.folder_id = None
        pipeline.account_id = _USER_ID
        pipeline.created_at = _NOW_STAMP
        pipeline.updated_at = _NOW_STAMP

        route_session = _make_route_session()
        auth_session = _make_auth_session()

        def override_session():
            yield route_session

        app.dependency_overrides[get_settings] = _make_settings
        app.dependency_overrides[get_db_session] = override_session
        app.dependency_overrides[_get_engine] = lambda: MagicMock()
        mock_plan = MagicMock()
        mock_plan.feature_enabled.return_value = True
        app.dependency_overrides[get_plan_context] = lambda: mock_plan
        try:
            with (
                patch("modulo.api.dependencies.get_or_create_engine", return_value=MagicMock()),
                patch(
                    "modulo.api.dependencies.get_or_create_session_factory",
                    return_value=_FakeFactory(auth_session),
                ),
                patch("modulo.auth.api_key.validate_api_key", return_value=_fake_key("operator")),
                patch(
                    "modulo.auth.dependencies.resolve_role_from_membership",
                    new=AsyncMock(return_value="operator"),
                ),
                patch("modulo.api.routes.pipelines.create_pipeline", return_value=pipeline),
            ):
                client = TestClient(app)
                resp = client.post(
                    "/api/v1/pipelines",
                    json={"name": "Apply Pipeline", "max_concurrent_runs": 5},
                    headers=_AUTH_HEADERS,
                )
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 201, resp.text
        assert resp.json()["name"] == "Apply Pipeline"

    def test_api_key_creates_trigger(self) -> None:
        """The pipeline-scoped trigger create (FAR-681: carries the declarative
        name) accepts the mk_ API-key bearer through the real stack."""
        target_pipeline_id = uuid.UUID("00000000-0000-0000-0000-0000000000aa")
        pipeline_row = MagicMock()
        pipeline_row.max_concurrent_runs = 5
        trigger_row = MagicMock()
        trigger_row.id = uuid.uuid4()
        trigger_row.organisation_id = _ORG_ID
        trigger_row.pipeline_id = target_pipeline_id
        trigger_row.name = "apply-trigger"
        trigger_row.trigger_type = "cron"
        trigger_row.active = True
        trigger_row.max_concurrent_runs = 1
        trigger_row.daily_spend_limit = None
        trigger_row.config_json = {}
        trigger_row.cron_expression = "0 3 * * *"
        trigger_row.cron_timezone = None
        trigger_row.last_fired_at = None
        trigger_row.next_fire_at = _NOW_STAMP
        trigger_row.account_id = _USER_ID

        route_session = _make_route_session()
        route_session.get = AsyncMock(return_value=pipeline_row)
        auth_session = _make_auth_session()

        def override_session():
            yield route_session

        app.dependency_overrides[get_settings] = _make_settings
        app.dependency_overrides[get_db_session] = override_session
        app.dependency_overrides[_get_engine] = lambda: MagicMock()
        mock_plan = MagicMock()
        mock_plan.feature_enabled.return_value = True
        app.dependency_overrides[get_plan_context] = lambda: mock_plan
        try:
            with (
                patch("modulo.api.dependencies.get_or_create_engine", return_value=MagicMock()),
                patch(
                    "modulo.api.dependencies.get_or_create_session_factory",
                    return_value=_FakeFactory(auth_session),
                ),
                patch("modulo.auth.api_key.validate_api_key", return_value=_fake_key("operator")),
                patch(
                    "modulo.auth.dependencies.resolve_role_from_membership",
                    new=AsyncMock(return_value="operator"),
                ),
            ):
                client = TestClient(app)
                resp = client.post(
                    f"/api/v1/pipelines/{target_pipeline_id}/triggers",
                    json={"name": "apply-trigger", "trigger_type": "cron", "cron_expression": "0 3 * * *"},
                    headers=_AUTH_HEADERS,
                )
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 201, resp.text
        assert resp.json()["name"] == "apply-trigger"
