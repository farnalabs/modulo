"""Unit tests for composite authoring flow.

Covers:
- Composite editor endpoints (GET/PUT /{id}/editor)
- Publish endpoint (POST /{id}/publish)
- Save-as-composite endpoint (POST /pipelines/{id}/save-as-composite)
- Auto-detection of parameter placeholders
"""

import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session
from modulo.api.main import app
from modulo.auth.dependencies import get_current_tenant_user, get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal, TenantPrincipal
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_TEMPLATE_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")
_PIPELINE_ID = uuid.UUID("00000000-0000-0000-0000-000000000004")
_AGENT_ID = uuid.UUID("00000000-0000-0000-0000-000000000005")
_NOW = datetime(2025, 1, 1, tzinfo=UTC)

# Shared mock session reference for save-as-composite tests
_mock_session: AsyncMock | None = None


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


def _make_template(**overrides: object) -> MagicMock:
    t = MagicMock()
    t.id = overrides.get("id", _TEMPLATE_ID)
    t.organisation_id = overrides.get("organisation_id", _ORG_ID)
    t.name = overrides.get("name", "Devil's Advocate")
    t.description = overrides.get("description")
    t.sub_pipeline_graph_json = overrides.get("sub_pipeline_graph_json", {"nodes": [], "edges": []})
    t.parameter_ports_json = overrides.get("parameter_ports_json", [])
    t.input_schema_id = overrides.get("input_schema_id")
    t.output_schema_id = overrides.get("output_schema_id")
    t.version = overrides.get("version", "1.0.0")
    t.account_id = _USER_ID
    t.created_at = _NOW
    t.updated_at = _NOW
    return t


def _make_mock_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


def _make_pipeline_mock(**overrides: object) -> MagicMock:
    p = MagicMock()
    p.id = overrides.get("id", _PIPELINE_ID)
    p.organisation_id = _ORG_ID
    p.name = overrides.get("name", "Test Pipeline")
    p.description = overrides.get("description")
    p.graph_nodes_json = overrides.get(
        "graph_nodes_json",
        [
            {
                "id": str(uuid.UUID("00000000-0000-0000-0000-000000000010")),
                "node_type": "agent",
                "agent_id": str(_AGENT_ID),
                "label": "Agent 1",
            },
            {
                "id": str(uuid.UUID("00000000-0000-0000-0000-000000000011")),
                "node_type": "manual",
                "label": "Manual 1",
            },
        ],
    )
    p.version = overrides.get("version", "1.0.0")
    p.rate_limit_config = overrides.get("rate_limit_config")
    p.max_duration_seconds = overrides.get("max_duration_seconds")
    p.archived_at = overrides.get("archived_at")
    p.snapshot_count = overrides.get("snapshot_count", 0)
    return p


def _make_agent_mock() -> MagicMock:
    a = MagicMock()
    a.id = _AGENT_ID
    a.organisation_id = _ORG_ID
    a.name = "Test Agent"
    a.prompt_template = (
        "Analyze this input and provide {{parameter.tone}} feedback with {{parameter.max_length}} words."
    )
    return a


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    global _mock_session
    _mock_session = _make_mock_session()

    session = _mock_session

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="testuser",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )
    app.dependency_overrides[get_current_tenant_user] = lambda: TenantPrincipal(
        username="testuser",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )
    yield TestClient(app)
    app.dependency_overrides.clear()
    _mock_session = None


@pytest.fixture
def unauth_client() -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_settings] = _make_settings
    yield TestClient(app)
    app.dependency_overrides.clear()


class TestCompositeEditor:
    """GET/PUT /api/v1/composite-templates/{id}/editor"""

    def test_get_editor_returns_graph(self, client: TestClient) -> None:
        template = _make_template(
            sub_pipeline_graph_json={
                "nodes": [{"id": "n1", "node_type": "agent"}],
                "edges": [{"id": "e1", "source": "n1", "target": "n2"}],
            },
        )
        with (
            patch("modulo.api.routes.composite_templates.get_composite_template", return_value=template),
            patch("modulo.api.routes.composite_templates.set_rls_org"),
        ):
            resp = client.get(f"/api/v1/composite-templates/{_TEMPLATE_ID}/editor")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["nodes"]) == 1
        assert data["nodes"][0]["id"] == "n1"
        assert len(data["edges"]) == 1

    def test_get_editor_404(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.composite_templates.get_composite_template", return_value=None),
            patch("modulo.api.routes.composite_templates.set_rls_org"),
        ):
            resp = client.get(f"/api/v1/composite-templates/{uuid.uuid4()}/editor")
        assert resp.status_code == 404

    def test_save_editor_saves_graph(self, client: TestClient) -> None:
        template = _make_template(
            sub_pipeline_graph_json={
                "nodes": [{"id": "n1", "node_type": "agent"}],
                "edges": [],
            },
        )
        with (
            patch("modulo.api.routes.composite_templates.get_composite_template", return_value=template),
            patch(
                "modulo.api.routes.composite_templates.update_composite_template", new=AsyncMock(return_value=template)
            ),
            patch("modulo.api.routes.composite_templates.set_rls_org"),
        ):
            resp = client.put(
                f"/api/v1/composite-templates/{_TEMPLATE_ID}/editor",
                json={
                    "nodes": [{"id": "n1", "node_type": "agent"}],
                    "edges": [],
                },
            )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["nodes"]) == 1

    def test_save_editor_404(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.composite_templates.get_composite_template", return_value=None),
            patch("modulo.api.routes.composite_templates.set_rls_org"),
        ):
            resp = client.put(
                f"/api/v1/composite-templates/{uuid.uuid4()}/editor",
                json={"nodes": [], "edges": []},
            )
        assert resp.status_code == 404


class TestCompositePublish:
    """POST /api/v1/composite-templates/{id}/publish"""

    def test_publish_sets_version_default(self, client: TestClient) -> None:
        template = _make_template(version="1.0.0")
        with (
            patch("modulo.api.routes.composite_templates.update_composite_template", return_value=template),
            patch("modulo.api.routes.composite_templates.set_rls_org"),
        ):
            resp = client.post(f"/api/v1/composite-templates/{_TEMPLATE_ID}/publish", json={})
        assert resp.status_code == 200
        data = resp.json()
        assert data["version"] == "1.0.0"
        assert data["published"] is True

    def test_publish_with_custom_version(self, client: TestClient) -> None:
        template = _make_template(version="2.0.0")
        with (
            patch("modulo.api.routes.composite_templates.update_composite_template", return_value=template),
            patch("modulo.api.routes.composite_templates.set_rls_org"),
        ):
            resp = client.post(
                f"/api/v1/composite-templates/{_TEMPLATE_ID}/publish",
                json={"version": "2.0.0"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["version"] == "2.0.0"

    def test_publish_404(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.composite_templates.update_composite_template", return_value=None),
            patch("modulo.api.routes.composite_templates.set_rls_org"),
        ):
            resp = client.post(f"/api/v1/composite-templates/{uuid.uuid4()}/publish", json={})
        assert resp.status_code == 404

    def test_publish_unauthorized(self, unauth_client: TestClient) -> None:
        resp = unauth_client.post(f"/api/v1/composite-templates/{_TEMPLATE_ID}/publish", json={})
        assert resp.status_code in (401, 403)


class TestSaveAsComposite:
    """POST /api/v1/pipelines/{id}/save-as-composite"""

    def test_save_as_composite_creates_template(self, client: TestClient) -> None:
        pipeline = _make_pipeline_mock()
        template = _make_template(name="Saved Composite", version="0.1.0")

        empty_execute = MagicMock()
        empty_execute.scalars.return_value.all.return_value = []

        selected_ids = [
            "00000000-0000-0000-0000-000000000010",
            "00000000-0000-0000-0000-000000000011",
        ]

        with (
            patch("modulo.api.routes.pipelines.get_pipeline", return_value=pipeline),
            patch("modulo.api.routes.pipelines.set_rls_org"),
            patch("modulo.api.routes.pipelines.set_rls_user_context"),
            patch("modulo.api.routes.pipelines.create_composite_template", return_value=template),
        ):
            assert _mock_session is not None
            _mock_session.execute = AsyncMock(return_value=empty_execute)
            resp = client.post(
                f"/api/v1/pipelines/{_PIPELINE_ID}/save-as-composite",
                json={
                    "name": "Saved Composite",
                    "description": "A saved composite",
                    "selected_node_ids": selected_ids,
                },
            )
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "Saved Composite"
        assert data["version"] == "0.1.0"

    def test_save_as_composite_auto_detects_parameters(self, client: TestClient) -> None:
        pipeline = _make_pipeline_mock()
        agent = _make_agent_mock()
        template = _make_template(name="Param Composite", version="0.1.0")

        scalars_result = MagicMock()
        scalars_result.scalars.return_value.all.return_value = [agent]

        selected_ids = ["00000000-0000-0000-0000-000000000010"]

        with (
            patch("modulo.api.routes.pipelines.get_pipeline", return_value=pipeline),
            patch("modulo.api.routes.pipelines.set_rls_org"),
            patch("modulo.api.routes.pipelines.set_rls_user_context"),
            patch("modulo.api.routes.pipelines.create_composite_template", return_value=template),
        ):
            assert _mock_session is not None
            _mock_session.execute = AsyncMock(return_value=scalars_result)
            resp = client.post(
                f"/api/v1/pipelines/{_PIPELINE_ID}/save-as-composite",
                json={
                    "name": "Param Composite",
                    "selected_node_ids": selected_ids,
                },
            )
        assert resp.status_code == 201
        data = resp.json()
        ports = data.get("parameter_ports", [])
        port_names = [p["name"] for p in ports]
        assert "tone" in port_names
        assert "max_length" in port_names

    def test_save_as_composite_no_valid_nodes_returns_422(self, client: TestClient) -> None:
        pipeline = _make_pipeline_mock()

        with (
            patch("modulo.api.routes.pipelines.get_pipeline", return_value=pipeline),
            patch("modulo.api.routes.pipelines.set_rls_org"),
            patch("modulo.api.routes.pipelines.set_rls_user_context"),
        ):
            resp = client.post(
                f"/api/v1/pipelines/{_PIPELINE_ID}/save-as-composite",
                json={
                    "name": "Test",
                    "selected_node_ids": [str(uuid.uuid4())],
                },
            )
        assert resp.status_code == 422

    def test_save_as_composite_pipeline_not_found(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.pipelines.get_pipeline", return_value=None),
            patch("modulo.api.routes.pipelines.set_rls_org"),
            patch("modulo.api.routes.pipelines.set_rls_user_context"),
        ):
            resp = client.post(
                f"/api/v1/pipelines/{_PIPELINE_ID}/save-as-composite",
                json={
                    "name": "Test",
                    "selected_node_ids": [str(uuid.uuid4())],
                },
            )
        assert resp.status_code == 404

    def test_save_as_composite_unauthorized(self, unauth_client: TestClient) -> None:
        resp = unauth_client.post(
            f"/api/v1/pipelines/{_PIPELINE_ID}/save-as-composite",
            json={"name": "Test", "selected_node_ids": [str(uuid.uuid4())]},
        )
        assert resp.status_code in (401, 403)

    def test_save_as_composite_below_operator_denied(self, client: TestClient) -> None:
        app.dependency_overrides[get_current_tenant_user] = lambda: TenantPrincipal(
            username="viewer",
            organisation_id=_ORG_ID,
            account_id=_USER_ID,
            org_role="viewer",
        )
        resp = client.post(
            f"/api/v1/pipelines/{_PIPELINE_ID}/save-as-composite",
            json={"name": "Test", "selected_node_ids": [str(uuid.uuid4())]},
        )
        assert resp.status_code == 403
        assert "Permission 'pipeline.create'" in resp.json()["detail"]

    def test_save_as_composite_carries_team_scope_gate(self) -> None:
        from tests.unit.api.route_introspection import get_mutating_routes, get_permission_tag

        route = next(
            r for r in get_mutating_routes(app) if r.path == "/api/v1/pipelines/{pipeline_id}/save-as-composite"
        )
        tag = get_permission_tag(route)
        assert tag is not None, "save-as-composite lost its permission tags"
        assert any(t["permission"] == "pipeline.create" for t in tag["tags"])
        assert any(t["permission_kind"] == "team_scope" for t in tag["tags"])


class TestCompositeDetectParams:
    """POST /api/v1/composite-templates/detect-params"""

    def test_detects_placeholders_across_node_prompts(self, client: TestClient) -> None:
        nodes = [
            {
                "id": "n1",
                "node_type": "agent",
                "prompt_template": "Respond with {{parameter.tone}} feedback.",
            },
            {
                "id": "n2",
                "node_type": "agent",
                "prompt": "Cap length at {{parameter.max_length}} words.",
            },
        ]
        resp = client.post(
            "/api/v1/composite-templates/detect-params",
            json={"node_ids": ["n1", "n2"], "nodes": nodes},
        )
        assert resp.status_code == 200
        ports = resp.json()["ports"]
        by_name = {p["name"]: p for p in ports}
        assert set(by_name) == {"tone", "max_length"}
        assert by_name["tone"]["label"] == "Tone"
        assert by_name["tone"]["target_injection"] == {
            "mode": "prompt_replace",
            "node_id": "n1",
            "injection_point": "prompt_template",
        }
        assert by_name["max_length"]["target_injection"]["node_id"] == "n2"

    def test_deduplicates_placeholders_across_nodes(self, client: TestClient) -> None:
        nodes = [
            {"id": "n1", "prompt_template": "Use {{parameter.tone}} here."},
            {"id": "n2", "prompt_template": "Also use {{parameter.tone}}."},
        ]
        resp = client.post(
            "/api/v1/composite-templates/detect-params",
            json={"nodes": nodes},
        )
        assert resp.status_code == 200
        ports = resp.json()["ports"]
        assert len(ports) == 1
        assert ports[0]["name"] == "tone"
        assert ports[0]["target_injection"]["node_id"] == "n1"

    def test_returns_empty_when_no_placeholders(self, client: TestClient) -> None:
        nodes = [
            {"id": "n1", "prompt_template": "No placeholders here."},
            {"id": "n2", "prompt": 42},
        ]
        resp = client.post(
            "/api/v1/composite-templates/detect-params",
            json={"nodes": nodes},
        )
        assert resp.status_code == 200
        assert not resp.json()["ports"]

    def test_unauthorized(self, unauth_client: TestClient) -> None:
        resp = unauth_client.post(
            "/api/v1/composite-templates/detect-params",
            json={"nodes": []},
        )
        assert resp.status_code in (401, 403)


class TestSaveAsCompositeCredentialStorage:
    """FAR-1374: save-as-composite persists the REAL credential values.

    The write-side mask was removed: the template stores the pipeline's
    declared environment as-is (parity with top-level pipeline graphs, which
    apply zero write-side masking and mask on read). Storing the mask sentinel
    would propagate it into run snapshots where it executes as a literal
    credential and clobbers the host-injected value upstream. Every READ
    surface keeps masking (FAR-1181), and a mask sentinel arriving on the
    write itself is refused 422 before anything persists.
    """

    _FAKE_CREDENTIAL = "FAKE_CREDENTIAL_FOR_TEST"

    def _credential_pipeline(self, *, env_token: str) -> MagicMock:
        return _make_pipeline_mock(
            graph_nodes_json=[
                {
                    "id": "00000000-0000-0000-0000-000000000010",
                    "node_type": "agent",
                    "agent_id": str(_AGENT_ID),
                    "label": "Agent 1",
                    "env_vars": {"GITHUB_TOKEN": env_token, "APP_URL": "https://example.com"},
                    "context_files": {"/tmp/creds.txt": f"token={env_token}"},
                },
                {
                    "id": "00000000-0000-0000-0000-000000000011",
                    "node_type": "manual",
                    "label": "Manual 1",
                    "env_vars": {"PLAIN": "not-sensitive"},
                },
            ],
        )

    def test_save_as_composite_persists_real_credential_values(self, client: TestClient) -> None:
        pipeline = self._credential_pipeline(env_token=self._FAKE_CREDENTIAL)
        template = _make_template(name="Secret Composite", version="0.1.0")
        create_mock = AsyncMock(return_value=template)
        empty_execute = MagicMock()
        empty_execute.scalars.return_value.all.return_value = []

        with (
            patch("modulo.api.routes.pipelines.get_pipeline", return_value=pipeline),
            patch("modulo.api.routes.pipelines.set_rls_org"),
            patch("modulo.api.routes.pipelines.set_rls_user_context"),
            patch("modulo.api.routes.pipelines.create_composite_template", new=create_mock),
        ):
            assert _mock_session is not None
            _mock_session.execute = AsyncMock(return_value=empty_execute)
            resp = client.post(
                f"/api/v1/pipelines/{_PIPELINE_ID}/save-as-composite",
                json={
                    "name": "Secret Composite",
                    "selected_node_ids": [
                        "00000000-0000-0000-0000-000000000010",
                        "00000000-0000-0000-0000-000000000011",
                    ],
                },
            )
        assert resp.status_code == 201
        graph = create_mock.call_args.kwargs["sub_pipeline_graph_json"]
        assert set(graph.keys()) == {"nodes", "edges"}
        persisted_first = graph["nodes"][0]
        # The REAL value is stored - no write-side masking (FAR-1374).
        assert persisted_first["env_vars"] == {
            "GITHUB_TOKEN": self._FAKE_CREDENTIAL,
            "APP_URL": "https://example.com",
        }
        assert persisted_first["context_files"] == {"/tmp/creds.txt": f"token={self._FAKE_CREDENTIAL}"}
        # Non-sensitive env keys pass through untouched (no over-masking).
        assert graph["nodes"][1]["env_vars"] == {"PLAIN": "not-sensitive"}
        # The source pipeline node dict is not mutated by the save.
        assert pipeline.graph_nodes_json[0]["env_vars"]["GITHUB_TOKEN"] == self._FAKE_CREDENTIAL

    def test_save_as_composite_response_and_read_surface_carry_no_raw_secret(self, client: TestClient) -> None:
        """The read invariant survives: neither the 201 body nor a follow-up
        GET exposes the stored credential - the template read masks it."""
        from modulo.api.middleware.sensitive_mask import SENSITIVE_VALUE_MASK

        pipeline = self._credential_pipeline(env_token=self._FAKE_CREDENTIAL)
        # The GET masks on the sensitive-KEY tier (GITHUB_TOKEN), the tier that
        # applies to any value regardless of its shape.
        template = _make_template(
            name="Secret Composite",
            version="0.1.0",
            sub_pipeline_graph_json={
                "nodes": [
                    {
                        "id": "00000000-0000-0000-0000-000000000010",
                        "node_type": "agent",
                        "agent_id": str(_AGENT_ID),
                        "label": "Agent 1",
                        "env_vars": {
                            "GITHUB_TOKEN": self._FAKE_CREDENTIAL,
                            "APP_URL": "https://example.com",
                        },
                    }
                ],
                "edges": [],
            },
        )
        # The shared helper leaves unset columns as auto-MagicMocks; the GET
        # response model coerces them, so pin the nullable one explicitly.
        template.parameter_schema_id = None
        create_mock = AsyncMock(return_value=template)
        empty_execute = MagicMock()
        empty_execute.scalars.return_value.all.return_value = []

        with (
            patch("modulo.api.routes.pipelines.get_pipeline", return_value=pipeline),
            patch("modulo.api.routes.pipelines.set_rls_org"),
            patch("modulo.api.routes.pipelines.set_rls_user_context"),
            patch("modulo.api.routes.pipelines.create_composite_template", new=create_mock),
        ):
            assert _mock_session is not None
            _mock_session.execute = AsyncMock(return_value=empty_execute)
            resp = client.post(
                f"/api/v1/pipelines/{_PIPELINE_ID}/save-as-composite",
                json={
                    "name": "Secret Composite",
                    "selected_node_ids": ["00000000-0000-0000-0000-000000000010"],
                },
            )
        assert resp.status_code == 201
        # The save-as-composite response never carries the graph at all.
        assert self._FAKE_CREDENTIAL not in resp.text

        with (
            patch(
                "modulo.api.routes.composite_templates.get_composite_template",
                return_value=template,
            ),
            patch("modulo.api.routes.composite_templates.set_rls_org"),
        ):
            read_resp = client.get(f"/api/v1/composite-templates/{_TEMPLATE_ID}")
        assert read_resp.status_code == 200
        assert self._FAKE_CREDENTIAL not in read_resp.text
        node = read_resp.json()["sub_pipeline_graph_json"]["nodes"][0]
        assert node["env_vars"]["GITHUB_TOKEN"] == SENSITIVE_VALUE_MASK

    def test_save_as_composite_rejects_mask_sentinel_with_422(self, client: TestClient) -> None:
        """A mask sentinel in the pipeline's nodes is refused before persisting."""
        from modulo.api.middleware.sensitive_mask import SENSITIVE_VALUE_MASK

        pipeline = self._credential_pipeline(env_token=SENSITIVE_VALUE_MASK)
        create_mock = AsyncMock()
        empty_execute = MagicMock()
        empty_execute.scalars.return_value.all.return_value = []

        with (
            patch("modulo.api.routes.pipelines.get_pipeline", return_value=pipeline),
            patch("modulo.api.routes.pipelines.set_rls_org"),
            patch("modulo.api.routes.pipelines.set_rls_user_context"),
            patch("modulo.api.routes.pipelines.create_composite_template", new=create_mock),
        ):
            assert _mock_session is not None
            _mock_session.execute = AsyncMock(return_value=empty_execute)
            resp = client.post(
                f"/api/v1/pipelines/{_PIPELINE_ID}/save-as-composite",
                json={
                    "name": "Degraded Composite",
                    "selected_node_ids": ["00000000-0000-0000-0000-000000000010"],
                },
            )
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert "COMPOSITE_SUBGRAPH_MASKED_CREDENTIAL" in detail
        assert "GITHUB_TOKEN" in detail
        create_mock.assert_not_called()
