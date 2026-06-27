"""Unit tests for /api/v1/admin/trigger-events endpoint."""

import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from modulo.api.dependencies import get_db_session
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_NOW = datetime(2025, 1, 1, tzinfo=UTC)


def _make_mock_event(**overrides: object) -> MagicMock:
    e = MagicMock()
    e.id = overrides.get("id", uuid.uuid4())
    e.trigger_id = overrides.get("trigger_id", uuid.uuid4())
    e.trigger_type = overrides.get("trigger_type", "manual")
    e.validation_result = overrides.get("validation_result", "accepted")
    e.received_at = overrides.get("received_at", _NOW)
    e.created_at = overrides.get("created_at", _NOW)
    e.run_id = overrides.get("run_id", None)
    e.error_detail = overrides.get("error_detail", None)
    return e


def _make_event_result(events: list[MagicMock]) -> MagicMock:
    r = MagicMock()
    r.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=events)))
    r.scalar = MagicMock(return_value=len(events))
    return r


def _make_mock_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    session.execute = AsyncMock(return_value=_make_event_result([]))
    return session


@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    def override_settings() -> Settings:
        return Settings(
            database_url="postgresql+asyncpg://localhost/test",
            secret_key="a" * 32,
            fernet_key="a" * 32,
            modulo_admin_password="testpass",
        )

    app.dependency_overrides[get_settings] = override_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="admin", organisation_id=_ORG_ID, user_id=_USER_ID, org_role="admin"
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture()
def unauth_client() -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_settings] = lambda: Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key="a" * 32,
        fernet_key="a" * 32,
        modulo_admin_password="testpass",
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture()
def operator_client() -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    def override_settings() -> Settings:
        return Settings(
            database_url="postgresql+asyncpg://localhost/test",
            secret_key="a" * 32,
            fernet_key="a" * 32,
            modulo_admin_password="testpass",
        )

    app.dependency_overrides[get_settings] = override_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="operator", organisation_id=_ORG_ID, user_id=_USER_ID, org_role="operator"
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


class TestListTriggerEvents:
    URL = "/api/v1/admin/trigger-events"

    def test_returns_200_with_events(self, client: TestClient) -> None:
        event = _make_mock_event()
        with patch("modulo.api.routes.admin_triggers.set_rls_org"):
            session = _make_mock_session()
            event_result = _make_event_result([event])
            count_result = MagicMock()
            count_result.scalar = MagicMock(return_value=1)
            session.execute = AsyncMock(side_effect=[event_result, count_result])

            async def override_session() -> AsyncGenerator[AsyncMock, None]:
                yield session

            client.app.dependency_overrides[get_db_session] = override_session
            resp = client.get(self.URL)

        assert resp.status_code == 200
        body = resp.json()
        assert len(body["items"]) == 1
        assert body["total"] == 1
        assert body["items"][0]["trigger_type"] == "manual"
        assert body["items"][0]["validation_result"] == "accepted"

    def test_returns_200_with_filters(self, client: TestClient) -> None:
        event = _make_mock_event(trigger_type="webhook", validation_result="hmac_failed")
        with patch("modulo.api.routes.admin_triggers.set_rls_org"):
            session = _make_mock_session()
            event_result = _make_event_result([event])
            count_result = MagicMock()
            count_result.scalar = MagicMock(return_value=1)
            session.execute = AsyncMock(side_effect=[event_result, count_result])

            async def override_session() -> AsyncGenerator[AsyncMock, None]:
                yield session

            client.app.dependency_overrides[get_db_session] = override_session
            resp = client.get(
                self.URL,
                params={"trigger_type": "webhook", "validation_result": "hmac_failed"},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert len(body["items"]) == 1
        assert body["items"][0]["trigger_type"] == "webhook"
        assert body["items"][0]["validation_result"] == "hmac_failed"

    def test_returns_empty_when_no_events(self, client: TestClient) -> None:
        with patch("modulo.api.routes.admin_triggers.set_rls_org"):
            session = _make_mock_session()
            event_result = _make_event_result([])
            count_result = MagicMock()
            count_result.scalar = MagicMock(return_value=0)
            session.execute = AsyncMock(side_effect=[event_result, count_result])

            async def override_session() -> AsyncGenerator[AsyncMock, None]:
                yield session

            client.app.dependency_overrides[get_db_session] = override_session
            resp = client.get(self.URL)

        assert resp.status_code == 200
        body = resp.json()
        assert len(body["items"]) == 0
        assert body["total"] == 0

    def test_operator_returns_403(self, operator_client: TestClient) -> None:
        resp = operator_client.get(self.URL)
        assert resp.status_code == 403

    def test_unauthorized_returns_4xx(self, unauth_client: TestClient) -> None:
        resp = unauth_client.get(self.URL)
        assert resp.status_code in (401, 403)
