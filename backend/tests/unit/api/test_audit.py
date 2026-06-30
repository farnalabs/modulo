"""Unit tests for /api/v1/admin/audit endpoints."""

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
_NOW = datetime(2025, 6, 1, tzinfo=UTC)


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
        username="admin",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture()
def unauth_client() -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_settings] = _make_settings
    yield TestClient(app)
    app.dependency_overrides.clear()


def _make_event(
    event_id: uuid.UUID | None = None,
    event_type: str = "pipeline.run",
    actor_user_id: str | None = str(_USER_ID),
    resource_type: str | None = "pipeline",
    resource_id: str | None = str(uuid.uuid4()),
    payload: dict | None = None,
    request_id: str | None = "req-123",
    previous_hash: str | None = "abc",
    created_at: datetime | None = _NOW,
):
    e = MagicMock()
    e.id = event_id or uuid.uuid4()
    e.event_type = event_type
    e.actor_user_id = uuid.UUID(actor_user_id) if actor_user_id else None
    e.resource_type = resource_type
    e.resource_id = uuid.UUID(resource_id) if resource_id else None
    e.payload_json = payload or {"key": "value"}
    e.request_id = request_id
    e.previous_hash = previous_hash
    e.created_at = created_at or _NOW
    return e


class TestListAuditEvents:
    URL = "/api/v1/admin/audit"

    def test_list_returns_paginated_events(self, client: TestClient) -> None:
        events = [_make_event() for _ in range(3)]
        with (
            patch(
                "modulo.api.routes.audit.list_audit_events",
                return_value={
                    "items": [
                        {
                            "id": str(e.id),
                            "event_type": e.event_type,
                            "actor_user_id": str(e.actor_user_id) if e.actor_user_id else None,
                            "resource_type": e.resource_type,
                            "resource_id": str(e.resource_id) if e.resource_id else None,
                            "payload_json": e.payload_json,
                            "request_id": e.request_id,
                            "previous_hash": e.previous_hash,
                            "created_at": e.created_at.isoformat() if e.created_at else None,
                        }
                        for e in events
                    ],
                    "total": 3,
                    "next_cursor": None,
                    "prev_cursor": str(events[0].id),
                    "limit": 50,
                },
            ),
            patch("modulo.api.routes.audit.set_rls_org"),
        ):
            resp = client.get(self.URL)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 3
        assert len(data["items"]) == 3
        assert data["items"][0]["event_type"] == "pipeline.run"

    def test_list_with_cursor_pagination(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.audit.list_audit_events") as mock_list,
            patch("modulo.api.routes.audit.set_rls_org"),
        ):
            mock_list.return_value = {
                "items": [],
                "total": 0,
                "next_cursor": None,
                "prev_cursor": None,
                "limit": 50,
            }
            resp = client.get(f"{self.URL}?cursor=00000000-0000-0000-0000-000000000010&limit=25")
        assert resp.status_code == 200
        _, kwargs = mock_list.call_args
        assert kwargs.get("cursor") == "00000000-0000-0000-0000-000000000010"
        assert kwargs.get("limit") == 25

    def test_list_with_event_type_filter(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.audit.list_audit_events") as mock_list,
            patch("modulo.api.routes.audit.set_rls_org"),
        ):
            mock_list.return_value = {
                "items": [],
                "total": 0,
                "next_cursor": None,
                "prev_cursor": None,
                "limit": 50,
            }
            resp = client.get(f"{self.URL}?event_type=pipeline.run")
        assert resp.status_code == 200
        _, kwargs = mock_list.call_args
        assert kwargs.get("event_type") == "pipeline.run"

    def test_list_with_user_id_filter(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.audit.list_audit_events") as mock_list,
            patch("modulo.api.routes.audit.set_rls_org"),
        ):
            mock_list.return_value = {
                "items": [],
                "total": 0,
                "next_cursor": None,
                "prev_cursor": None,
                "limit": 50,
            }
            resp = client.get(f"{self.URL}?user_id={_USER_ID}")
        assert resp.status_code == 200
        _, kwargs = mock_list.call_args
        assert kwargs.get("actor_user_id") == _USER_ID

    def test_list_with_entity_type_filter(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.audit.list_audit_events") as mock_list,
            patch("modulo.api.routes.audit.set_rls_org"),
        ):
            mock_list.return_value = {
                "items": [],
                "total": 0,
                "next_cursor": None,
                "prev_cursor": None,
                "limit": 50,
            }
            resp = client.get(f"{self.URL}?entity_type=pipeline")
        assert resp.status_code == 200
        _, kwargs = mock_list.call_args
        assert kwargs.get("resource_type") == "pipeline"

    def test_list_with_date_range(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.audit.list_audit_events") as mock_list,
            patch("modulo.api.routes.audit.set_rls_org"),
        ):
            mock_list.return_value = {
                "items": [],
                "total": 0,
                "next_cursor": None,
                "prev_cursor": None,
                "limit": 50,
            }
            resp = client.get(f"{self.URL}?from_date=2025-01-01T00:00:00Z&to_date=2025-12-31T23:59:59Z")
        assert resp.status_code == 200
        _, kwargs = mock_list.call_args
        assert kwargs.get("from_date") == "2025-01-01T00:00:00Z"
        assert kwargs.get("to_date") == "2025-12-31T23:59:59Z"

    def test_unauthorized_returns_4xx(self, unauth_client: TestClient) -> None:
        resp = unauth_client.get(self.URL)
        assert resp.status_code in (401, 403)


class TestBatchDetail:
    URL = "/api/v1/admin/audit/batch-detail"

    def test_batch_detail_returns_events(self, client: TestClient) -> None:
        event_id = str(uuid.uuid4())
        with (
            patch(
                "modulo.api.routes.audit.get_audit_events_batch",
                return_value=[
                    {
                        "id": event_id,
                        "event_type": "pipeline.run",
                        "actor_user_id": str(_USER_ID),
                        "resource_type": "pipeline",
                        "resource_id": str(uuid.uuid4()),
                        "payload_json": {"key": "value"},
                        "request_id": "req-123",
                        "previous_hash": "abc",
                        "created_at": "2025-06-01T00:00:00+00:00",
                    }
                ],
            ),
            patch("modulo.api.routes.audit.set_rls_org"),
        ):
            resp = client.post(self.URL, json={"event_ids": [event_id]})
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["id"] == event_id
        assert data[0]["event_type"] == "pipeline.run"

    def test_batch_detail_empty_ids(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.audit.get_audit_events_batch", return_value=[]),
            patch("modulo.api.routes.audit.set_rls_org"),
        ):
            resp = client.post(self.URL, json={"event_ids": []})
        assert resp.status_code == 200
        assert resp.json() == []

    def test_unauthorized_returns_4xx(self, unauth_client: TestClient) -> None:
        resp = unauth_client.post(self.URL, json={"event_ids": []})
        assert resp.status_code in (401, 403)
