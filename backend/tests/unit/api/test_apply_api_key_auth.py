"""Real-dependency-stack API-key (``mk_``) auth tests for the endpoints the
``modulo apply`` CLI uses (FAR-681 slice 1).

Unlike the per-route fixtures in test_schemas_endpoint.py /
test_model_backends_endpoint.py (which override ``get_current_user``), these
tests exercise the REAL ``require_permission_any_credential`` dependency chain —
including ``get_current_tenant_user_or_api_key``'s mk_ key resolution — so an
``Authorization: Bearer mk_...`` header genuinely authenticates and creates a
schema + a model backend.

FAR-681 QA additions: the /agents list endpoint (the apply fetch path), the
any-credential team-gate matrix (team-scoped keys carry a HARD team boundary),
and graph-save gate parity between the apply graph path and the dedicated
graph endpoint.
"""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from cryptography.fernet import Fernet
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.api.team_scope import TeamScopedResource
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal, TenantPrincipal
from modulo.core.graph_validator._types import ValidationResult
from modulo.settings import Settings, get_settings
from tests.unit.api.mock_session import configure_mock_session

_FERNET_KEY = Fernet.generate_key().decode()
_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_TEAM_A_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
_TEAM_B_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a2")
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


def _fake_key(role: str = "operator", team_id: uuid.UUID | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        name="apply-key",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        role=role,
        team_id=team_id,
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
        # FAR-681 duplicate-name check: the (pipeline, name) lookup must MISS
        # (no live duplicate) — a bare MagicMock result's .first() is truthy
        # and would falsely 409 the create.
        duplicate_miss = MagicMock()
        duplicate_miss.first = MagicMock(return_value=None)
        route_session.execute = AsyncMock(return_value=duplicate_miss)
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


def _agent_row() -> SimpleNamespace:
    """All AgentResponse fields (from_attributes model validates the namespace)."""
    return SimpleNamespace(
        id=uuid.uuid4(),
        organisation_id=_ORG_ID,
        name="worker",
        description=None,
        is_executable=True,
        input_schema_id=None,
        input_schema_version=None,
        output_schema_id=None,
        output_schema_version=None,
        prompt_template="p",
        prompt_version_history=[],
        model_backend_id=None,
        connector_type_refs=[],
        evals=None,
        retry_policy={},
        token_budget=None,
        max_input_length=None,
        library_id=None,
        prompt_always_visible=False,
        required_environment_capabilities=[],
        template_id=None,
        agent_command=None,
        agent_commands=None,
        account_id=_USER_ID,
        created_at=_NOW_STAMP,
        updated_at=_NOW_STAMP,
    )


class TestApiKeyListsAgents:
    def test_api_key_lists_agents(self) -> None:
        """FAR-681 QA (real-dependency fetch path): GET /agents accepts the
        mk_ bearer through the REAL stack. The endpoint used to be JWT-only,
        so the executor's /agents fetch 401'd for API-key principals and
        aborted the ENTIRE apply run (schemas/backends included)."""
        agent = _agent_row()
        listing = SimpleNamespace(items=[agent], total=1, page=1, page_size=20)
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
                patch("modulo.api.routes.agents.list_agents", return_value=listing),
            ):
                client = TestClient(app)
                resp = client.get("/api/v1/agents", headers=_AUTH_HEADERS)
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 200, resp.text
        assert [a["name"] for a in resp.json()["items"]] == ["worker"]


def _make_gate_session(*, membership_found: object = None) -> AsyncMock:
    """Session for the team-gate dependency: begin() CM + a configurable
    membership query result (``.first()`` truthiness decides membership)."""
    session = AsyncMock()
    configure_mock_session(session)
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    result = MagicMock()
    result.first = MagicMock(return_value=membership_found)
    session.execute = AsyncMock(return_value=result)
    return session


def _team_gate_app(provider):
    """A minimal app with one GET route behind the ANY-CREDENTIAL team gate."""
    from modulo.api.dependencies import require_team_membership_or_admin_any_credential

    test_app = FastAPI()

    @test_app.get("/team-resource")
    async def route(
        request: Request,
        principal: TenantPrincipal = require_team_membership_or_admin_any_credential(provider),
    ) -> dict:
        return {"principal_team_id": str(principal.team_id) if principal.team_id else None}

    return test_app


class TestTeamGateKeyBoundary:
    """FAR-681 QA matrix: the any-credential team gate honours the API key's
    OWN team scope (not just the key OWNER's memberships) — the MCP boundary.
    """

    def _run_gate(
        self,
        test_app: FastAPI,
        key: SimpleNamespace,
        gate_session: AsyncMock,
    ):
        def override_session():
            yield gate_session

        auth_session = _make_auth_session()
        test_app.dependency_overrides[get_db_session] = override_session
        try:
            with (
                patch("modulo.api.dependencies.get_or_create_engine", return_value=MagicMock()),
                patch(
                    "modulo.api.dependencies.get_or_create_session_factory",
                    return_value=_FakeFactory(auth_session),
                ),
                patch("modulo.auth.api_key.validate_api_key", return_value=key),
                patch(
                    "modulo.auth.dependencies.resolve_role_from_membership",
                    new=AsyncMock(return_value="operator"),
                ),
                patch("modulo.api.dependencies.set_rls_org", new=AsyncMock()),
                patch("modulo.api.dependencies.set_rls_user_context", new=AsyncMock()),
            ):
                client = TestClient(test_app)
                return client.get("/team-resource", headers=_AUTH_HEADERS)
        finally:
            test_app.dependency_overrides.clear()

    def test_scoped_key_matching_team_is_allowed(self) -> None:
        async def provider(request: Request, session) -> TeamScopedResource:
            return TeamScopedResource(owner_team_id=_TEAM_A_ID, visibility="team")

        test_app = _team_gate_app(provider)
        resp = self._run_gate(test_app, _fake_key(team_id=_TEAM_A_ID), _make_gate_session())
        assert resp.status_code == 200, resp.text
        assert resp.json()["principal_team_id"] == str(_TEAM_A_ID)

    def test_scoped_key_other_team_is_denied(self) -> None:
        """A team-A-scoped key must not touch a team-B resource EVEN when its
        owner is a member of team B (the key OWNER's memberships are
        irrelevant) — 403 with the MCP-style boundary message."""

        async def provider(request: Request, session) -> TeamScopedResource:
            return TeamScopedResource(owner_team_id=_TEAM_B_ID, visibility="team")

        test_app = _team_gate_app(provider)
        # The OWNER IS a member of team B — the boundary must still deny.
        resp = self._run_gate(test_app, _fake_key(team_id=_TEAM_A_ID), _make_gate_session(membership_found=MagicMock()))
        assert resp.status_code == 403, resp.text
        assert str(_TEAM_A_ID) in resp.json()["detail"]
        assert str(_TEAM_B_ID) in resp.json()["detail"]

    def test_unscoped_key_uses_owner_membership(self) -> None:
        """An org-wide key keeps today's behaviour: the gate checks the key
        OWNER's membership (member -> allowed, non-member -> 403)."""

        async def provider(request: Request, session) -> TeamScopedResource:
            return TeamScopedResource(owner_team_id=_TEAM_B_ID, visibility="team")

        test_app = _team_gate_app(provider)
        member = self._run_gate(test_app, _fake_key(team_id=None), _make_gate_session(membership_found=MagicMock()))
        assert member.status_code == 200, member.text
        non_member = self._run_gate(test_app, _fake_key(team_id=None), _make_gate_session(membership_found=None))
        assert non_member.status_code == 403, non_member.text
        assert "Not a member of the team that owns this resource" in non_member.json()["detail"]


class TestGraphSaveGateParity:
    def test_invalid_graph_rejected_identically_via_update_patch_and_graph_endpoint(self) -> None:
        """FAR-681 QA (gate parity): the apply graph path (PATCH /{id} with
        graph_json) must enforce the SAME save-time gates as the dedicated
        graph endpoint — an invalid graph is rejected 422 on BOTH (before the
        fix, the apply path saved without running _validate_graph_save, so
        GUARDRAIL_CAP_EXCEEDED / REDACT_CORRECT_BLOCKED /
        HITL_GATE_DESCRIPTION_REQUIRED never fired there)."""
        node_id = str(uuid.uuid4())
        nodes = [
            {
                "id": node_id,
                "node_type": "hitl",
                "hitl_config": {"label": "Review"},
                "position": {"x": 0, "y": 0},
            }
        ]
        edges: list[dict] = []
        validation = ValidationResult()
        validation.error(
            "HITL_GATE_DESCRIPTION_REQUIRED",
            "HITL gate on node requires a human-provided description",
            node_id=node_id,
        )
        pipeline = MagicMock()
        pipeline.id = uuid.uuid4()
        pipeline.organisation_id = _ORG_ID
        pipeline.name = "Apply Pipeline"
        pipeline.description = None
        pipeline.visibility = "org"
        pipeline.owner_team_id = None
        pipeline.max_concurrent_runs = 5

        route_session = _make_route_session()

        def override_session():
            yield route_session

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
        try:
            with (
                patch("modulo.api.routes.pipelines.set_rls_org"),
                patch("modulo.api.routes.pipelines.set_rls_user_context"),
                patch("modulo.api.routes.pipelines.get_pipeline", return_value=pipeline),
                patch("modulo.api.routes.pipelines._resolve_graph_references", return_value=([], [])),
                patch("modulo.api.routes.pipelines.replace_pipeline_graph", return_value=(nodes, edges)),
                patch(
                    "modulo.api.routes.pipelines.GraphValidator.validate_definition",
                    return_value=validation,
                ),
                patch(
                    "modulo.db.crud.guardrail_config.load_pipeline_guardrail_rows",
                    new=AsyncMock(return_value=[]),
                ),
            ):
                client = TestClient(app)
                resp_apply_path = client.patch(
                    f"/api/v1/pipelines/{pipeline.id}",
                    json={"graph_json": {"nodes": nodes, "edges": edges}},
                )
                resp_graph_endpoint = client.patch(
                    f"/api/v1/pipelines/{pipeline.id}/graph",
                    json={"nodes": nodes, "edges": edges},
                )
        finally:
            app.dependency_overrides.clear()

        assert resp_apply_path.status_code == 422, resp_apply_path.text
        assert "human-provided description" in resp_apply_path.json()["detail"]
        assert resp_graph_endpoint.status_code == 422, resp_graph_endpoint.text
        assert "human-provided description" in resp_graph_endpoint.json()["detail"]
