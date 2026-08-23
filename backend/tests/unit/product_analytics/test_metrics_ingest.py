"""Unit tests for POST /api/v1/metrics/events (FAR-355)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Generator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import Insert as PGInsert

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.auth.dependencies import get_current_tenant_user, get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal, TenantPrincipal
from modulo.settings import Settings, get_settings
from tests.unit.api.mock_session import configure_mock_session

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")


def _insert_calls(mock_session: AsyncMock) -> list:
    """Return the pg_insert statements actually handed to ``session.execute``."""
    return [c.args[0] for c in mock_session.execute.call_args_list if isinstance(c.args[0], PGInsert)]


def _inserted_payload(mock_session: AsyncMock, index: int = -1) -> dict:
    """Compile the captured insert at *index* and return its ``payload`` value.

    The mock ``session.execute`` discards the staged row, so we recover the
    observable write by compiling the captured ``pg_insert(...).values(...)``
    statement against the Postgres dialect and reading the bound ``payload``.
    """
    stmt = _insert_calls(mock_session)[index]
    compiled = stmt.compile(dialect=postgresql.dialect())
    payload = compiled.params.get("payload")
    assert isinstance(payload, dict), "captured insert must carry a payload dict"
    return payload


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
        # Prove-the-fix: at the cap, every api_error event must be SKIPPED, so
        # no insert statement is ever handed to the session (the cap guard
        # ``continue``s before building the pg_insert). If the guard is removed,
        # this assertion fails because 5 inserts would appear.
        assert not _insert_calls(mock_session), "api_error events must be skipped once the daily cap is reached"

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
        # Prove-the-fix: with headroom (95 + 5 = 100, the cap inclusive check is
        # ``>=``), all 5 events must be staged. If the cap guard is broken, fewer
        # inserts would be produced.
        assert len(_insert_calls(mock_session)) == 5, "all 5 api_error events under the cap must be staged"


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
        # Prove-the-fix: the core behaviour of ``_sanitize_route_template`` is
        # the staged payload's ``route`` being replaced with ``"unknown"`` (the
        # mock execute discards the row, so we compile the captured insert).
        payload = _inserted_payload(mock_session)
        assert payload["route"] == "unknown", "unmatched route must be sanitized to 'unknown' in the staged payload"
        assert payload["status"] == 500, "non-route fields must be preserved"

    @patch("modulo.api.routes.metrics_ingest._api_error_count_today")
    @patch("modulo.api.routes.metrics_ingest.get_organisation")
    @patch("modulo.api.routes.metrics_ingest.set_rls_user_context")
    @patch("modulo.api.routes.metrics_ingest.set_rls_org")
    def test_registered_route_template_preserved(
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
        event["payload"] = {"route": "/api/v1/metrics/events", "status": 500}
        resp = _post_events(client, [event])
        assert resp.status_code == 204
        # Prove-the-fix: FastAPI 0.130+ stores included routers lazily as
        # _IncludedRouter wrappers (path=None), so the sanitizer must walk the
        # nested router tree or it would never match a registered template and
        # would degrade every api_error route to "unknown".
        payload = _inserted_payload(mock_session)
        assert payload["route"] == "/api/v1/metrics/events", (
            f"registered route template must be preserved, got {payload.get('route')!r}"
        )
        assert payload["status"] == 500, "non-route fields must be preserved"
