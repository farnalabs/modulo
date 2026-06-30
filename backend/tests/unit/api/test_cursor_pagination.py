"""Tests for cursor-based pagination utility and endpoint integration."""

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.db.crud.base import PageResult
from modulo.db.crud.pagination import CursorPage, CursorPaginator
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
    )


def _make_mock_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


# ---------------------------------------------------------------------------
# CursorPaginator unit tests
# ---------------------------------------------------------------------------


class TestCursorEncoding:
    @staticmethod
    def test_encode_decode_roundtrip() -> None:
        record_id = uuid.uuid4()
        dt = _NOW

        encoded = CursorPaginator.encode_cursor(dt, record_id)
        decoded_val, decoded_id = CursorPaginator.decode_cursor(encoded)

        decoded_dt = datetime.fromisoformat(decoded_val)
        assert decoded_dt == dt
        assert decoded_id == record_id

    @staticmethod
    def test_encode_decode_string_value() -> None:
        record_id = uuid.uuid4()
        encoded = CursorPaginator.encode_cursor("some-string-value", record_id)
        decoded_val, decoded_id = CursorPaginator.decode_cursor(encoded)

        assert decoded_val == "some-string-value"
        assert decoded_id == record_id

    @staticmethod
    def test_cursor_is_opaque_string() -> None:
        record_id = uuid.uuid4()
        encoded = CursorPaginator.encode_cursor(_NOW, record_id)
        assert isinstance(encoded, str)
        assert len(encoded) > 0
        assert ":" not in encoded  # base64 encoded, no colons visible

    @staticmethod
    def test_different_values_produce_different_cursors() -> None:
        rid1 = uuid.uuid4()
        rid2 = uuid.uuid4()
        t1 = _NOW
        t2 = _NOW + timedelta(seconds=1)

        c1 = CursorPaginator.encode_cursor(t1, rid1)
        c2 = CursorPaginator.encode_cursor(t2, rid2)

        assert c1 != c2

    @staticmethod
    def test_decode_malformed_cursor_raises() -> None:
        with pytest.raises(Exception):
            CursorPaginator.decode_cursor("not-base64!!!")

# ---------------------------------------------------------------------------
# API endpoint integration tests
# ---------------------------------------------------------------------------


@pytest.fixture()
def client() -> TestClient:
    mock_session = _make_mock_session()

    async def override_session():
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
    yield TestClient(app)
    app.dependency_overrides.clear()


class TestPipelinesEndpointCursor:
    """Test cursor pagination integration on GET /api/v1/pipelines."""

    @staticmethod
    def test_list_with_cursor_param(client: TestClient) -> None:
        """cursor query param is accepted and passed to CRUD."""
        page_result = PageResult(
            items=[], total=0, page=1, page_size=20,
            next_cursor=None, has_more=False,
        )

        with (
            patch("modulo.api.routes.pipelines.list_pipelines", return_value=page_result),
            patch("modulo.api.routes.pipelines.set_rls_org"),
            patch("modulo.api.routes.pipelines.set_rls_user_context"),
        ):
            resp = client.get("/api/v1/pipelines?cursor=abc123&page_size=10")

        assert resp.status_code == 200

    @staticmethod
    def test_list_without_cursor_backward_compat(client: TestClient) -> None:
        """Without cursor param, offset pagination still works."""
        pipeline = MagicMock()
        pipeline.id = uuid.uuid4()
        pipeline.organisation_id = _ORG_ID
        pipeline.name = "Test Pipeline"
        pipeline.description = None
        pipeline.visibility = "org"
        pipeline.max_concurrent_runs = 5
        pipeline.lock_wait_timeout_seconds = 300
        pipeline.node_timeout_seconds = 300
        pipeline.run_context_defaults = {}
        pipeline.default_autonomy_level = "manual_approval"
        pipeline.snapshot_count = 0
        pipeline.created_by = uuid.uuid4()
        pipeline.created_at = _NOW
        pipeline.updated_at = _NOW

        page_result = PageResult(
            items=[pipeline], total=1, page=1, page_size=20,
        )

        with (
            patch("modulo.api.routes.pipelines.list_pipelines", return_value=page_result),
            patch("modulo.api.routes.pipelines.set_rls_org"),
            patch("modulo.api.routes.pipelines.set_rls_user_context"),
        ):
            resp = client.get("/api/v1/pipelines")

        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert body["page"] == 1
        assert body["page_size"] == 20
        assert len(body["items"]) == 1
        assert body["items"][0]["name"] == "Test Pipeline"
        assert body["next_cursor"] is None
        assert body["has_more"] is False

    @staticmethod
    def test_list_returns_cursor_fields_when_present(client: TestClient) -> None:
        """Response includes next_cursor and has_more when cursor pagination used."""
        pipeline = MagicMock()
        pipeline.id = uuid.uuid4()
        pipeline.organisation_id = _ORG_ID
        pipeline.name = "Pipeline A"
        pipeline.description = None
        pipeline.visibility = "org"
        pipeline.max_concurrent_runs = 5
        pipeline.lock_wait_timeout_seconds = 300
        pipeline.node_timeout_seconds = 300
        pipeline.run_context_defaults = {}
        pipeline.default_autonomy_level = "manual_approval"
        pipeline.snapshot_count = 0
        pipeline.created_by = uuid.uuid4()
        pipeline.created_at = _NOW
        pipeline.updated_at = _NOW

        page_result = PageResult(
            items=[pipeline], total=5, page=1, page_size=1,
            next_cursor="some-cursor-value", has_more=True,
        )

        with (
            patch("modulo.api.routes.pipelines.list_pipelines", return_value=page_result),
            patch("modulo.api.routes.pipelines.set_rls_org"),
            patch("modulo.api.routes.pipelines.set_rls_user_context"),
        ):
            resp = client.get("/api/v1/pipelines?cursor=prev-cursor&page_size=1")

        assert resp.status_code == 200
        body = resp.json()
        assert body["next_cursor"] == "some-cursor-value"
        assert body["has_more"] is True


class TestConnectorsEndpointCursor:
    """Test cursor pagination integration on GET /api/v1/connectors."""

    @staticmethod
    def test_list_with_cursor(client: TestClient) -> None:
        page_result = PageResult(
            items=[], total=0, page=1, page_size=20,
            next_cursor="next-page", has_more=False,
        )

        with (
            patch("modulo.api.routes.connectors.list_connector_instances", return_value=page_result),
            patch("modulo.api.routes.connectors.set_rls_org"),
            patch("modulo.api.routes.connectors.set_rls_user_context"),
        ):
            resp = client.get("/api/v1/connectors?cursor=abc")

        assert resp.status_code == 200
        body = resp.json()
        assert "next_cursor" in body
        assert "has_more" in body


class TestLibraryEndpointCursor:
    """Test cursor pagination integration on GET /api/v1/libraries."""

    @staticmethod
    def test_list_with_cursor(client: TestClient) -> None:
        page_result = PageResult(
            items=[], total=0, page=1, page_size=20,
            next_cursor=None, has_more=False,
        )

        with (
            patch("modulo.api.routes.library.list_primitives", return_value=page_result),
            patch("modulo.api.routes.library.set_rls_org"),
            patch("modulo.api.routes.library.set_rls_user_context"),
        ):
            resp = client.get("/api/v1/libraries?cursor=xyz&page_size=5")

        assert resp.status_code == 200
        body = resp.json()
        assert "next_cursor" in body
        assert "has_more" in body


class TestPageResultBackwardCompat:
    """Existing code that constructs PageResult without cursor fields still works."""

    @staticmethod
    def test_page_result_defaults() -> None:
        result = PageResult(items=["a", "b"], total=2, page=1, page_size=10)
        assert result.next_cursor is None
        assert result.has_more is False

    @staticmethod
    def test_page_result_with_cursor_fields() -> None:
        result = PageResult(
            items=["a"], total=10, page=1, page_size=1,
            next_cursor="cursor123", has_more=True,
        )
        assert result.next_cursor == "cursor123"
        assert result.has_more is True


class TestCursorPageModel:
    """CursorPage Pydantic model works as expected."""

    @staticmethod
    def test_basic_construction() -> None:
        page = CursorPage(items=[1, 2, 3], next_cursor="abc", has_more=True, total=10)
        assert page.items == [1, 2, 3]
        assert page.next_cursor == "abc"
        assert page.has_more is True
        assert page.total == 10

    @staticmethod
    def test_defaults() -> None:
        page = CursorPage(items=[])
        assert page.next_cursor is None
        assert page.has_more is False
        assert page.total is None
