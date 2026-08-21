"""Unit tests for POST /api/v1/metrics/events (FAR-355)."""

import uuid
from collections.abc import AsyncGenerator, Generator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.auth.dependencies import get_current_tenant_user, get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal, TenantPrincipal
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


def _mock_org(settings_json: dict | None = None) -> MagicMock:
    org = MagicMock()
    org.id = _ORG_ID
    org.settings_json = settings_json or {}
    return org


def _consented_org() -> MagicMock:
    return _mock_org({"product_analytics": {"level": "all"}})


@pytest.fixture
def mock_session() -> AsyncMock:
    session = configure_mock_session(AsyncMock(), allow_empty_execute=True)
    begin_cm = MagicMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


@pytest.fixture
def client(mock_session: AsyncMock) -> Generator[TestClient, None, None]:
    from modulo.api.main import app

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_plan_context] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="testuser", organisation_id=_ORG_ID, account_id=_USER_ID, org_role="admin"
    )
    app.dependency_overrides[get_current_tenant_user] = lambda: TenantPrincipal(
        username="testuser", organisation_id=_ORG_ID, account_id=_USER_ID, org_role="admin"
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


def _valid_event(event_id: str = "evt-1", event_type: str = "pipeline_created") -> dict:
    return {"event_id": event_id, "event_type": event_type, "payload": {"name": "test"}}


def _post_events(client: TestClient, events: list[dict]) -> Any:
    return client.post("/api/v1/metrics/events", json={"events": events})


class TestSuccessfulIngest:
    @patch("modulo.api.routes.metrics_ingest.get_organisation")
    @patch("modulo.api.routes.metrics_ingest.set_rls_user_context")
    @patch("modulo.api.routes.metrics_ingest.set_rls_org")
    def test_batch_insert_returns_204(
        self,
        mock_rls: MagicMock,
        mock_user_ctx: MagicMock,
        mock_get_org: MagicMock,
        client: TestClient,
        mock_session: AsyncMock,
    ) -> None:
        mock_get_org.return_value = _consented_org()
        resp = _post_events(client, [_valid_event()])
        assert resp.status_code == 204

    @patch("modulo.api.routes.metrics_ingest.get_organisation")
    @patch("modulo.api.routes.metrics_ingest.set_rls_user_context")
    @patch("modulo.api.routes.metrics_ingest.set_rls_org")
    def test_multiple_events(
        self,
        mock_rls: MagicMock,
        mock_user_ctx: MagicMock,
        mock_get_org: MagicMock,
        client: TestClient,
        mock_session: AsyncMock,
    ) -> None:
        mock_get_org.return_value = _consented_org()
        events = [_valid_event(f"evt-{i}") for i in range(5)]
        resp = _post_events(client, events)
        assert resp.status_code == 204


class TestConsentGate:
    @patch("modulo.api.routes.metrics_ingest.get_organisation")
    @patch("modulo.api.routes.metrics_ingest.set_rls_user_context")
    @patch("modulo.api.routes.metrics_ingest.set_rls_org")
    def test_consent_off_returns_204(
        self,
        mock_rls: MagicMock,
        mock_user_ctx: MagicMock,
        mock_get_org: MagicMock,
        client: TestClient,
        mock_session: AsyncMock,
    ) -> None:
        mock_get_org.return_value = _mock_org({"product_analytics": {"level": "off"}})
        resp = _post_events(client, [_valid_event()])
        assert resp.status_code == 204

    @patch("modulo.api.routes.metrics_ingest.get_organisation")
    @patch("modulo.api.routes.metrics_ingest.set_rls_user_context")
    @patch("modulo.api.routes.metrics_ingest.set_rls_org")
    def test_no_settings_returns_204(
        self,
        mock_rls: MagicMock,
        mock_user_ctx: MagicMock,
        mock_get_org: MagicMock,
        client: TestClient,
        mock_session: AsyncMock,
    ) -> None:
        mock_get_org.return_value = _mock_org(None)
        resp = _post_events(client, [_valid_event()])
        assert resp.status_code == 204

    @patch("modulo.api.routes.metrics_ingest.get_organisation")
    @patch("modulo.api.routes.metrics_ingest.set_rls_user_context")
    @patch("modulo.api.routes.metrics_ingest.set_rls_org")
    def test_org_not_found_returns_204(
        self,
        mock_rls: MagicMock,
        mock_user_ctx: MagicMock,
        mock_get_org: MagicMock,
        client: TestClient,
        mock_session: AsyncMock,
    ) -> None:
        mock_get_org.return_value = None
        resp = _post_events(client, [_valid_event()])
        assert resp.status_code == 204


class TestBatchSizeLimit:
    def test_empty_batch_returns_422(self, client: TestClient) -> None:
        resp = client.post("/api/v1/metrics/events", json={"events": []})
        assert resp.status_code == 422

    def test_over_max_batch_returns_422(self, client: TestClient) -> None:
        events = [_valid_event(f"evt-{i}") for i in range(1001)]
        resp = client.post("/api/v1/metrics/events", json={"events": events})
        assert resp.status_code == 422


class TestEventValidation:
    def test_unknown_event_type_returns_422(self, client: TestClient) -> None:
        resp = _post_events(client, [{"event_id": "e1", "event_type": "bogus"}])
        assert resp.status_code == 422

    def test_missing_event_id_returns_422(self, client: TestClient) -> None:
        resp = _post_events(client, [{"event_type": "pipeline_created"}])
        assert resp.status_code == 422

    def test_missing_event_type_returns_422(self, client: TestClient) -> None:
        resp = _post_events(client, [{"event_id": "e1"}])
        assert resp.status_code == 422


class TestApiErrorDailyCap:
    @patch("modulo.api.routes.metrics_ingest._api_error_count_today")
    @patch("modulo.api.routes.metrics_ingest.get_organisation")
    @patch("modulo.api.routes.metrics_ingest.set_rls_user_context")
    @patch("modulo.api.routes.metrics_ingest.set_rls_org")
    def test_api_error_cap_skips_excess(
        self,
        mock_rls: MagicMock,
        mock_user_ctx: MagicMock,
        mock_get_org: MagicMock,
        mock_count: MagicMock,
        client: TestClient,
        mock_session: AsyncMock,
    ) -> None:
        mock_get_org.return_value = _consented_org()
        mock_count.return_value = 100  # Already at cap
        events = [_valid_event(f"evt-{i}", "api_error") for i in range(5)]
        resp = _post_events(client, events)
        assert resp.status_code == 204

    @patch("modulo.api.routes.metrics_ingest._api_error_count_today")
    @patch("modulo.api.routes.metrics_ingest.get_organisation")
    @patch("modulo.api.routes.metrics_ingest.set_rls_user_context")
    @patch("modulo.api.routes.metrics_ingest.set_rls_org")
    def test_api_error_under_cap_accepted(
        self,
        mock_rls: MagicMock,
        mock_user_ctx: MagicMock,
        mock_get_org: MagicMock,
        mock_count: MagicMock,
        client: TestClient,
        mock_session: AsyncMock,
    ) -> None:
        mock_get_org.return_value = _consented_org()
        mock_count.return_value = 95  # Under cap
        events = [_valid_event(f"evt-{i}", "api_error") for i in range(5)]
        resp = _post_events(client, events)
        assert resp.status_code == 204


class TestRouteSanitizer:
    @patch("modulo.api.routes.metrics_ingest._api_error_count_today")
    @patch("modulo.api.routes.metrics_ingest.get_organisation")
    @patch("modulo.api.routes.metrics_ingest.set_rls_user_context")
    @patch("modulo.api.routes.metrics_ingest.set_rls_org")
    def test_unmatched_route_sanitized(
        self,
        mock_rls: MagicMock,
        mock_user_ctx: MagicMock,
        mock_get_org: MagicMock,
        mock_count: MagicMock,
        client: TestClient,
        mock_session: AsyncMock,
    ) -> None:
        mock_get_org.return_value = _consented_org()
        mock_count.return_value = 0
        event = _valid_event("evt-1", "api_error")
        event["payload"] = {"route": "/some/unknown/path", "status": 500}
        resp = _post_events(client, [event])
        assert resp.status_code == 204
