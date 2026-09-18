"""Unit tests for the determination API endpoints.

Covers the two route handlers (``GET /api/v1/determination`` and
``POST /api/v1/determination/draft``): happy paths, the empty/placeholder-org
scan, operator-role enforcement (403 for non-operators), unauthenticated 401,
and the full error-handling matrix (ProgrammingError → 501, SQLAlchemyError →
503, ConnectorDecryptError → 502, generic Exception → 500).
"""

import uuid
from collections.abc import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError

from modulo.api.dependencies import get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_tenant_user
from modulo.auth.jwt import TenantPrincipal
from modulo.connectors.base import ConnectorType
from modulo.core.connector_hub import ConnectorDecryptError
from modulo.determination.draft import DraftNode, PipelineDraft
from modulo.determination.inference import Finding
from modulo.determination.scanner import ScanSample
from modulo.settings import Settings, get_settings
from tests.unit.api.mock_session import configure_mock_session

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


def _make_principal(role: str) -> TenantPrincipal:
    return TenantPrincipal(
        username="operator" if role == "operator" else "viewer",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role=role,
    )


def _make_mock_session() -> AsyncMock:
    session = AsyncMock()
    configure_mock_session(session)
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


def _mock_page(items: list) -> MagicMock:
    page = MagicMock()
    page.items = items
    page.total = len(items)
    page.page = 1
    page.page_size = 100
    return page


def _mock_connector_instance(connector_type: str = "github") -> MagicMock:
    ci = MagicMock()
    ci.id = uuid.uuid4()
    ci.name = "Test Connector"
    ci.connector_type_id = connector_type
    ci.config_json = {}
    ci.credentials_ciphertext = b"encrypted"
    ci.visibility = "org"
    ci.allowed_operations = None
    return ci


def _mock_hub_context() -> MagicMock:
    """Build a ``ConnectorHub``-shaped async context manager class double."""
    hub = AsyncMock()
    hub.initialise = AsyncMock()
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=hub)
    cm.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=cm)


def _github_sample() -> ScanSample:
    return ScanSample(
        connector_id=uuid.uuid4(),
        connector_type=ConnectorType.GITHUB,
        resource="repos",
        records=[{"name": "owner/repo1"}],
        sample_count=1,
    )


def _overview_finding() -> Finding:
    return Finding(
        category="overview",
        finding="SDLC stages detected: development",
        evidence="1 repository accessible",
        confidence="medium",
    )


def _draft() -> PipelineDraft:
    return PipelineDraft(
        nodes=[
            DraftNode(id="start", node_type="placeholder", label="Start"),
            DraftNode(id="end", node_type="placeholder", label="End"),
        ],
        edges=[],
        findings=[_overview_finding()],
        automation_suggestions=[],
    )


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_current_tenant_user] = lambda: _make_principal("operator")
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def viewer_client() -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_current_tenant_user] = lambda: _make_principal("viewer")
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


class TestRunDetermination:
    def test_returns_200_with_scan_results(self, client: TestClient) -> None:
        ci = _mock_connector_instance("github")
        sample = _github_sample()
        finding = _overview_finding()

        with (
            patch("modulo.api.routes.determination.list_connector_instances", return_value=_mock_page([ci])),
            patch("modulo.api.routes.determination.set_rls_org"),
            patch("modulo.api.routes.determination.create_secrets_backend"),
            patch("modulo.api.routes.determination.ConnectorHub", _mock_hub_context()),
            patch("modulo.api.routes.determination.run_scan", new_callable=AsyncMock, return_value=[sample]),
            patch("modulo.api.routes.determination.infer", return_value=[finding]),
        ):
            resp = client.get("/api/v1/determination")

        assert resp.status_code == 200
        data = resp.json()
        assert data["summary"] == "SDLC stages detected: development"
        assert len(data["samples"]) == 1
        assert data["samples"][0]["resource"] == "repos"
        assert data["samples"][0]["sample_count"] == 1
        assert len(data["findings"]) == 1
        assert data["findings"][0]["category"] == "overview"

    def test_determination_threads_session_into_secrets_backend(self, client: TestClient) -> None:
        """Regression (FAR-519): the ConnectorHub must be built with a secrets
        backend carrying the DB session so connector credentials actually
        decrypt.

        Without ``session=session`` ``FernetSecretsBackend`` raises
        ``RuntimeError('no DB session')`` on ``get_secret`` and every connector
        is silently skipped — producing a blank determination scan instead of a
        real one."""
        ci = _mock_connector_instance("github")
        sample = _github_sample()
        finding = _overview_finding()
        captured: dict[str, object] = {}

        def fake_create_backend(*args: object, **kwargs: object) -> object:
            captured["session"] = kwargs.get("session")
            return MagicMock()

        with (
            patch("modulo.api.routes.determination.list_connector_instances", return_value=_mock_page([ci])),
            patch("modulo.api.routes.determination.set_rls_org"),
            patch("modulo.api.routes.determination.create_secrets_backend", fake_create_backend),
            patch("modulo.api.routes.determination.ConnectorHub", _mock_hub_context()),
            patch("modulo.api.routes.determination.run_scan", new_callable=AsyncMock, return_value=[sample]),
            patch("modulo.api.routes.determination.infer", return_value=[finding]),
        ):
            resp = client.get("/api/v1/determination")

        assert resp.status_code == 200
        assert captured["session"] is not None, "secrets backend must be built with the DB session"

    def test_no_connectors_returns_empty_results(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.determination.list_connector_instances", return_value=_mock_page([])),
            patch("modulo.api.routes.determination.set_rls_org"),
            patch("modulo.api.routes.determination.create_secrets_backend"),
            patch("modulo.api.routes.determination.ConnectorHub", _mock_hub_context()),
            patch("modulo.api.routes.determination.run_scan", new_callable=AsyncMock, return_value=[]),
            patch(
                "modulo.api.routes.determination.infer",
                return_value=[
                    Finding(
                        category="overview",
                        finding="No SDLC stages could be detected from connected tools",
                        evidence="no data",
                        confidence="low",
                    )
                ],
            ),
        ):
            resp = client.get("/api/v1/determination")

        assert resp.status_code == 200
        data = resp.json()
        assert not data["samples"]
        assert data["summary"] == "No SDLC stages could be detected from connected tools"

    def test_unauthenticated_returns_4xx(self, unauth_client: TestClient) -> None:
        resp = unauth_client.get("/api/v1/determination")
        assert resp.status_code in (401, 403)

    def test_viewer_returns_403(self, viewer_client: TestClient) -> None:
        resp = viewer_client.get("/api/v1/determination")
        assert resp.status_code == 403

    def test_programming_error_returns_501(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.determination.list_connector_instances",
                side_effect=ProgrammingError("stmt", {}, Exception("missing table")),
            ),
            patch("modulo.api.routes.determination.set_rls_org"),
            patch("modulo.api.routes.determination.create_secrets_backend"),
        ):
            resp = client.get("/api/v1/determination")

        assert resp.status_code == 501

    def test_sqlalchemy_error_returns_503(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.determination.list_connector_instances",
                side_effect=SQLAlchemyError("connection lost"),
            ),
            patch("modulo.api.routes.determination.set_rls_org"),
            patch("modulo.api.routes.determination.create_secrets_backend"),
        ):
            resp = client.get("/api/v1/determination")

        assert resp.status_code == 503

    def test_decrypt_error_returns_502(self, client: TestClient) -> None:
        ci = _mock_connector_instance("github")
        hub_cls = _mock_hub_context()
        hub = hub_cls.return_value.__aenter__.return_value
        hub.initialise.side_effect = ConnectorDecryptError(ci.id)

        with (
            patch("modulo.api.routes.determination.list_connector_instances", return_value=_mock_page([ci])),
            patch("modulo.api.routes.determination.set_rls_org"),
            patch("modulo.api.routes.determination.create_secrets_backend"),
            patch("modulo.api.routes.determination.ConnectorHub", hub_cls),
        ):
            resp = client.get("/api/v1/determination")

        assert resp.status_code == 502

    def test_generic_exception_returns_500(self, client: TestClient) -> None:
        ci = _mock_connector_instance("github")

        with (
            patch("modulo.api.routes.determination.list_connector_instances", return_value=_mock_page([ci])),
            patch("modulo.api.routes.determination.set_rls_org"),
            patch("modulo.api.routes.determination.create_secrets_backend"),
            patch("modulo.api.routes.determination.ConnectorHub", _mock_hub_context()),
            patch("modulo.api.routes.determination.run_scan", new_callable=AsyncMock, return_value=[_github_sample()]),
            patch("modulo.api.routes.determination.infer", side_effect=RuntimeError("boom")),
        ):
            resp = client.get("/api/v1/determination")

        assert resp.status_code == 500
        assert resp.json()["detail"] == "Internal server error"


class TestCreateDeterminationDraft:
    def test_returns_200_with_draft_graph(self, client: TestClient) -> None:
        ci = _mock_connector_instance("github")
        sample = _github_sample()
        finding = _overview_finding()
        draft = _draft()

        with (
            patch("modulo.api.routes.determination.list_connector_instances", return_value=_mock_page([ci])),
            patch("modulo.api.routes.determination.set_rls_org"),
            patch("modulo.api.routes.determination.create_secrets_backend"),
            patch("modulo.api.routes.determination.ConnectorHub", _mock_hub_context()),
            patch("modulo.api.routes.determination.run_scan", new_callable=AsyncMock, return_value=[sample]),
            patch("modulo.api.routes.determination.infer", return_value=[finding]),
            patch("modulo.api.routes.determination.generate_draft", return_value=draft),
        ):
            resp = client.post("/api/v1/determination/draft")

        assert resp.status_code == 200
        data = resp.json()
        assert data["summary"] == "SDLC stages detected: development"
        assert len(data["nodes"]) == 2
        assert data["nodes"][0]["id"] == "start"
        assert not data["edges"]
        assert not data["automation_suggestions"]
        assert len(data["findings"]) == 1

    def test_unauthenticated_returns_4xx(self, unauth_client: TestClient) -> None:
        resp = unauth_client.post("/api/v1/determination/draft")
        assert resp.status_code in (401, 403)

    def test_viewer_returns_403(self, viewer_client: TestClient) -> None:
        resp = viewer_client.post("/api/v1/determination/draft")
        assert resp.status_code == 403

    def test_programming_error_returns_501(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.determination.list_connector_instances",
                side_effect=ProgrammingError("stmt", {}, Exception("missing table")),
            ),
            patch("modulo.api.routes.determination.set_rls_org"),
            patch("modulo.api.routes.determination.create_secrets_backend"),
        ):
            resp = client.post("/api/v1/determination/draft")

        assert resp.status_code == 501

    def test_sqlalchemy_error_returns_503(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.determination.list_connector_instances",
                side_effect=SQLAlchemyError("connection lost"),
            ),
            patch("modulo.api.routes.determination.set_rls_org"),
            patch("modulo.api.routes.determination.create_secrets_backend"),
        ):
            resp = client.post("/api/v1/determination/draft")

        assert resp.status_code == 503

    def test_decrypt_error_returns_502(self, client: TestClient) -> None:
        ci = _mock_connector_instance("github")
        hub_cls = _mock_hub_context()
        hub = hub_cls.return_value.__aenter__.return_value
        hub.initialise.side_effect = ConnectorDecryptError(ci.id)

        with (
            patch("modulo.api.routes.determination.list_connector_instances", return_value=_mock_page([ci])),
            patch("modulo.api.routes.determination.set_rls_org"),
            patch("modulo.api.routes.determination.create_secrets_backend"),
            patch("modulo.api.routes.determination.ConnectorHub", hub_cls),
        ):
            resp = client.post("/api/v1/determination/draft")

        assert resp.status_code == 502

    def test_generic_exception_returns_500(self, client: TestClient) -> None:
        ci = _mock_connector_instance("github")

        with (
            patch("modulo.api.routes.determination.list_connector_instances", return_value=_mock_page([ci])),
            patch("modulo.api.routes.determination.set_rls_org"),
            patch("modulo.api.routes.determination.create_secrets_backend"),
            patch("modulo.api.routes.determination.ConnectorHub", _mock_hub_context()),
            patch("modulo.api.routes.determination.run_scan", new_callable=AsyncMock, return_value=[_github_sample()]),
            patch("modulo.api.routes.determination.infer", return_value=[_overview_finding()]),
            patch("modulo.api.routes.determination.generate_draft", side_effect=RuntimeError("boom")),
        ):
            resp = client.post("/api/v1/determination/draft")

        assert resp.status_code == 500
        assert resp.json()["detail"] == "Internal server error"
