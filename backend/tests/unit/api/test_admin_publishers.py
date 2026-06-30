"""Unit tests for /api/v1/admin/publishers endpoints."""

import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from typing import ClassVar
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.db.crud.base import PageResult
from modulo.db.models.publisher import Publisher
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_NOW = datetime(2025, 6, 1, tzinfo=UTC)
_PUB_KEY = "ab" * 32


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


def _make_mock_publisher(
    pid: uuid.UUID | None = None,
    name: str = "Test Publisher",
    tier: str = "green",
) -> Publisher:
    pub = MagicMock(spec=Publisher)
    pub.id = pid or uuid.uuid4()
    pub.organisation_id = _ORG_ID
    pub.name = name
    pub.contact_email = "pub@test.com"
    pub.public_key_hex = _PUB_KEY
    pub.trust_tier = tier
    pub.verified_since = _NOW if tier == "green" else None
    pub.website_url = "https://test.com"
    pub.created_at = _NOW
    pub.updated_at = _NOW
    return pub


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


@pytest.fixture()
def operator_client() -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="operator",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="operator",
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


LIST_URL = "/api/v1/admin/publishers"


class TestListPublishers:
    def test_admin_lists_publishers_returns_200(self, client: TestClient) -> None:
        pubs = [_make_mock_publisher(name="Pub One"), _make_mock_publisher(name="Pub Two")]
        page_result = PageResult(items=pubs, total=2, page=1, page_size=20)
        with (
            patch("modulo.api.routes.admin.list_publishers", return_value=page_result),
            patch("modulo.api.routes.admin.set_rls_org"),
        ):
            resp = client.get(LIST_URL)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        assert len(data["items"]) == 2
        assert data["items"][0]["name"] == "Pub One"

    def test_operator_returns_403(self, operator_client: TestClient) -> None:
        resp = operator_client.get(LIST_URL)
        assert resp.status_code == 403

    def test_unauthorized_returns_4xx(self, unauth_client: TestClient) -> None:
        resp = unauth_client.get(LIST_URL)
        assert resp.status_code in (401, 403)

    def test_filters_by_tier(self, client: TestClient) -> None:
        pubs = [_make_mock_publisher(name="Green Pub", tier="green")]
        page_result = PageResult(items=pubs, total=1, page=1, page_size=20)
        with (
            patch("modulo.api.routes.admin.list_publishers", return_value=page_result),
            patch("modulo.api.routes.admin.set_rls_org"),
        ):
            resp = client.get(f"{LIST_URL}?trust_tier=green")
        assert resp.status_code == 200
        assert resp.json()["total"] == 1


CREATE_URL = "/api/v1/admin/publishers"


class TestCreatePublisher:
    PAYLOAD: ClassVar[dict[str, object]] = {
        "name": "New Publisher",
        "contact_email": "new@test.com",
        "public_key_hex": _PUB_KEY,
        "trust_tier": "green",
        "website_url": "https://new.com",
    }

    def test_admin_creates_publisher_returns_201(self, client: TestClient) -> None:
        pub = _make_mock_publisher(name="New Publisher", tier="green")
        with (
            patch("modulo.api.routes.admin.get_publisher_by_name", return_value=None),
            patch("modulo.api.routes.admin.get_publisher_by_key", return_value=None),
            patch("modulo.api.routes.admin.create_publisher", return_value=pub),
            patch("modulo.api.routes.admin.set_rls_org"),
        ):
            resp = client.post(CREATE_URL, json=self.PAYLOAD)
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "New Publisher"
        assert data["trust_tier"] == "green"
        assert data["website_url"] == "https://test.com"

    def test_operator_returns_403(self, operator_client: TestClient) -> None:
        resp = operator_client.post(CREATE_URL, json=self.PAYLOAD)
        assert resp.status_code == 403

    def test_unauthorized_returns_4xx(self, unauth_client: TestClient) -> None:
        resp = unauth_client.post(CREATE_URL, json=self.PAYLOAD)
        assert resp.status_code in (401, 403)

    def test_duplicate_name_returns_409(self, client: TestClient) -> None:
        existing = _make_mock_publisher(name="New Publisher")
        with (
            patch("modulo.api.routes.admin.get_publisher_by_name", return_value=existing),
            patch("modulo.api.routes.admin.set_rls_org"),
        ):
            resp = client.post(CREATE_URL, json=self.PAYLOAD)
        assert resp.status_code == 409

    def test_duplicate_key_returns_409(self, client: TestClient) -> None:
        existing = _make_mock_publisher(name="Other")
        with (
            patch("modulo.api.routes.admin.get_publisher_by_name", return_value=None),
            patch("modulo.api.routes.admin.get_publisher_by_key", return_value=existing),
            patch("modulo.api.routes.admin.set_rls_org"),
        ):
            resp = client.post(CREATE_URL, json=self.PAYLOAD)
        assert resp.status_code == 409

    def test_invalid_tier_returns_422(self, client: TestClient) -> None:
        bad = {**self.PAYLOAD, "trust_tier": "purple"}
        with patch("modulo.api.routes.admin.set_rls_org"):
            resp = client.post(CREATE_URL, json=bad)
        assert resp.status_code == 422

    def test_crud_raises_value_error_returns_422(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.admin.get_publisher_by_name", return_value=None),
            patch("modulo.api.routes.admin.get_publisher_by_key", return_value=None),
            patch("modulo.api.routes.admin.create_publisher", side_effect=ValueError("Invalid tier")),
            patch("modulo.api.routes.admin.set_rls_org"),
        ):
            resp = client.post(CREATE_URL, json=self.PAYLOAD)
        assert resp.status_code == 422


UPDATE_URL = "/api/v1/admin/publishers/00000000-0000-0000-0000-000000000003"


class TestUpdatePublisher:
    PAYLOAD: ClassVar[dict[str, object]] = {"name": "Updated", "trust_tier": "amber"}

    def test_admin_updates_publisher_returns_200(self, client: TestClient) -> None:
        pub = _make_mock_publisher(name="Updated", tier="amber")
        with (
            patch("modulo.api.routes.admin.get_publisher_by_name", return_value=None),
            patch("modulo.api.routes.admin.get_publisher_by_key", return_value=None),
            patch("modulo.api.routes.admin.crud_update_publisher", return_value=pub),
            patch("modulo.api.routes.admin.set_rls_org"),
        ):
            resp = client.put(UPDATE_URL, json=self.PAYLOAD)
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "Updated"
        assert data["trust_tier"] == "amber"

    def test_operator_returns_403(self, operator_client: TestClient) -> None:
        resp = operator_client.put(UPDATE_URL, json=self.PAYLOAD)
        assert resp.status_code == 403

    def test_unauthorized_returns_4xx(self, unauth_client: TestClient) -> None:
        resp = unauth_client.put(UPDATE_URL, json=self.PAYLOAD)
        assert resp.status_code in (401, 403)

    def test_not_found_returns_404(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.admin.get_publisher_by_name", return_value=None),
            patch("modulo.api.routes.admin.get_publisher_by_key", return_value=None),
            patch("modulo.api.routes.admin.crud_update_publisher", return_value=None),
            patch("modulo.api.routes.admin.set_rls_org"),
        ):
            resp = client.put(UPDATE_URL, json=self.PAYLOAD)
        assert resp.status_code == 404

    def test_duplicate_name_returns_409(self, client: TestClient) -> None:
        existing = _make_mock_publisher(name="Other Pub")
        with (
            patch("modulo.api.routes.admin.get_publisher_by_name", return_value=existing),
            patch("modulo.api.routes.admin.set_rls_org"),
        ):
            resp = client.put(UPDATE_URL, json={"name": "Other Pub"})
        assert resp.status_code == 409


DELETE_URL = "/api/v1/admin/publishers/00000000-0000-0000-0000-000000000003"


class TestDeletePublisher:
    def test_admin_deletes_publisher_returns_204(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.admin.crud_delete_publisher", return_value=True),
            patch("modulo.api.routes.admin.set_rls_org"),
        ):
            resp = client.delete(DELETE_URL)
        assert resp.status_code == 204

    def test_operator_returns_403(self, operator_client: TestClient) -> None:
        resp = operator_client.delete(DELETE_URL)
        assert resp.status_code == 403

    def test_unauthorized_returns_4xx(self, unauth_client: TestClient) -> None:
        resp = unauth_client.delete(DELETE_URL)
        assert resp.status_code in (401, 403)

    def test_not_found_returns_404(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.admin.crud_delete_publisher", return_value=False),
            patch("modulo.api.routes.admin.set_rls_org"),
        ):
            resp = client.delete(DELETE_URL)
        assert resp.status_code == 404
