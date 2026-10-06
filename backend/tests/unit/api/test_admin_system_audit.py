"""Unit tests for the system-admin read surface over ``system_audit_events`` (FAR-1538).

No Postgres: the route module's read helper is patched, so these tests assert the
API contract - the system-admin gate, the page envelope, query validation, filter
forwarding, and the DB-error mapping - while ``tests/unit/crud/test_system_audit_event.py``
asserts that the filters actually reach the SQL statement.
"""

from __future__ import annotations

import uuid
from collections.abc import Generator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings
from tests.unit.api.plan_stubs import all_features

_ORG = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER = uuid.UUID("00000000-0000-0000-0000-000000000002")
_URL = "/api/v1/admin/system-audit"


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key="a" * 32,
        fernet_key="a" * 32,
        modulo_admin_password="testpass",
    )


def _principal(*, system_admin: bool) -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        username="sysadmin@test" if system_admin else "admin@test",
        organisation_id=_ORG,
        account_id=_USER,
        org_role="admin",
        is_system_admin=system_admin,
    )


def _make_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = MagicMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


def _build(session: AsyncMock, principal: AuthenticatedPrincipal) -> TestClient:
    app.dependency_overrides.clear()
    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_plan_context] = lambda: all_features()

    async def override_session():
        yield session

    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_current_user] = lambda: principal
    return TestClient(app)


@pytest.fixture(autouse=True)
def _clean() -> Generator[None, None, None]:
    yield
    app.dependency_overrides.clear()


def _page_return_value() -> tuple[list[object], int]:
    event = MagicMock()
    event.id = uuid.UUID("11111111-1111-1111-1111-111111111111")
    event.event_type = "org_deletion_requested"
    event.org_id = _ORG
    event.actor_user_id = _USER
    event.resource_type = "organisation"
    event.resource_id = _ORG
    event.payload_json = {"organisation_id": str(_ORG)}
    event.request_id = "req-1"
    event.created_at = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    return [event], 1


def _patch_read(return_value: tuple[list[object], int] | None = None):
    return patch(
        "modulo.api.routes.admin_system_audit.list_system_audit_events",
        new=AsyncMock(return_value=return_value or _page_return_value()),
    )


class TestSystemAdminGate:
    def test_system_admin_can_list(self) -> None:
        session = _make_session()
        client = _build(session, _principal(system_admin=True))
        with _patch_read() as mock_read:
            resp = client.get(_URL)

        assert resp.status_code == 200
        body = resp.json()
        assert set(body) == {"items", "total", "page", "page_size"}
        assert body["total"] == 1
        assert body["page"] == 1
        assert body["page_size"] == 50
        assert body["items"][0]["event_type"] == "org_deletion_requested"
        assert body["items"][0]["org_id"] == str(_ORG)
        assert body["items"][0]["created_at"] == "2026-10-01T12:00:00+00:00"
        # The durable ledger is deliberately not hash-chained.
        assert "previous_hash" not in body["items"][0]
        session.begin.assert_called_once()
        mock_read.assert_awaited_once()

    def test_org_admin_without_system_admin_gets_403(self) -> None:
        session = _make_session()
        client = _build(session, _principal(system_admin=False))
        with _patch_read() as mock_read:
            resp = client.get(_URL)

        assert resp.status_code == 403
        mock_read.assert_not_awaited()

    def test_unauthenticated_gets_401(self) -> None:
        session = _make_session()
        client = _build(session, _principal(system_admin=True))
        # Drop the principal override: the real bearer-token dependency then
        # rejects a header-less request before the handler runs - a read
        # surface over cross-org history must never be reachable anonymously.
        app.dependency_overrides.pop(get_current_user, None)
        resp = client.get(_URL)
        assert resp.status_code == 401


class TestFiltersForwarded:
    def test_event_type_org_and_date_range_reach_the_read_helper(self) -> None:
        session = _make_session()
        client = _build(session, _principal(system_admin=True))
        org_id = uuid.uuid4()
        from_date = "2026-01-01T00:00:00Z"
        to_date = "2026-12-31T23:59:59Z"
        with _patch_read() as mock_read:
            resp = client.get(
                _URL,
                params={
                    "event_type": "org_deletion_completed",
                    "org_id": str(org_id),
                    "from_date": from_date,
                    "to_date": to_date,
                    "page": 3,
                    "page_size": 25,
                },
            )

        assert resp.status_code == 200
        kwargs = mock_read.call_args.kwargs
        assert kwargs["event_type"] == "org_deletion_completed"
        assert kwargs["org_id"] == org_id
        assert kwargs["from_date"] == datetime(2026, 1, 1, tzinfo=UTC)
        assert kwargs["to_date"] == datetime(2026, 12, 31, 23, 59, 59, tzinfo=UTC)
        assert kwargs["page"] == 3
        assert kwargs["page_size"] == 25
        # The envelope echoes the requested page, not a hard-coded first page.
        assert resp.json()["page"] == 3
        assert resp.json()["page_size"] == 25

    def test_invalid_org_id_is_422(self) -> None:
        session = _make_session()
        client = _build(session, _principal(system_admin=True))
        with _patch_read() as mock_read:
            resp = client.get(_URL, params={"org_id": "not-a-uuid"})

        assert resp.status_code == 422
        mock_read.assert_not_awaited()

    def test_page_and_page_size_bounds_are_enforced(self) -> None:
        session = _make_session()
        client = _build(session, _principal(system_admin=True))
        with _patch_read():
            assert client.get(_URL, params={"page": 0}).status_code == 422
            assert client.get(_URL, params={"page_size": 0}).status_code == 422
            assert client.get(_URL, params={"page_size": 201}).status_code == 422

    def test_no_filters_returns_the_first_default_page(self) -> None:
        session = _make_session()
        client = _build(session, _principal(system_admin=True))
        with _patch_read() as mock_read:
            resp = client.get(_URL)

        assert resp.status_code == 200
        kwargs = mock_read.call_args.kwargs
        assert kwargs["event_type"] is None
        assert kwargs["org_id"] is None
        assert kwargs["from_date"] is None
        assert kwargs["to_date"] is None
        assert kwargs["page"] == 1
        assert kwargs["page_size"] == 50


class TestDbErrorMapping:
    def test_programming_error_maps_to_501(self) -> None:
        session = _make_session()
        client = _build(session, _principal(system_admin=True))
        with patch(
            "modulo.api.routes.admin_system_audit.list_system_audit_events",
            new=AsyncMock(side_effect=ProgrammingError("", "", "")),
        ):
            resp = client.get(_URL)

        assert resp.status_code == 501

    def test_sqlalchemy_error_maps_to_503(self) -> None:
        session = _make_session()
        client = _build(session, _principal(system_admin=True))
        with patch(
            "modulo.api.routes.admin_system_audit.list_system_audit_events",
            new=AsyncMock(side_effect=SQLAlchemyError("connection lost")),
        ):
            resp = client.get(_URL)

        assert resp.status_code == 503

    def test_unexpected_error_maps_to_500(self) -> None:
        session = _make_session()
        client = _build(session, _principal(system_admin=True))
        with patch(
            "modulo.api.routes.admin_system_audit.list_system_audit_events",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ):
            resp = client.get(_URL)

        assert resp.status_code == 500
