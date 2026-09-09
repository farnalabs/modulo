"""Unit tests for library collection authoring lifecycle (FAR-760).

Unit tier: no DB — CRUD/service functions are patched at the route-module
boundary and the SQLAlchemy session is a contract-correct AsyncMock.
"""

import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.auth.permissions import PERMISSIONS
from modulo.core.library_service.primitive_types import (
    COLLECTION_PIN_TYPES,
    MAX_COLLECTION_PINS,
    PRIMITIVE_TYPES,
)
from modulo.settings import Settings, get_settings
from tests.unit.api.mock_session import configure_mock_session

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_NOW = datetime(2025, 1, 1, tzinfo=UTC)


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


def _make_mock_session() -> AsyncMock:
    session = configure_mock_session(AsyncMock())
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


def _principal(*, is_system_admin: bool = False) -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        username="testuser",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
        is_system_admin=is_system_admin,
    )


def _build_client(principal: AuthenticatedPrincipal) -> tuple[TestClient, AsyncMock]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: principal
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    test_client = TestClient(app)
    test_client.mock_session = mock_session  # type: ignore[attr-defined]
    return test_client, mock_session


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    test_client, _ = _build_client(_principal())
    yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def runner_client() -> Generator[TestClient, None, None]:
    runner_principal = AuthenticatedPrincipal(
        username="runner",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="runner",
    )
    test_client, _ = _build_client(runner_principal)
    yield test_client
    app.dependency_overrides.clear()


def _make_collection_primitive(
    *,
    pid: uuid.UUID | None = None,
    status: str | None = "draft",
    manifest_pins: list[dict] | None = None,
    slug: str = "my-collection",
) -> MagicMock:
    p = MagicMock()
    p.id = pid or uuid.uuid4()
    p.organisation_id = _ORG_ID
    p.source = "local"
    p.primitive_type = "library_collection"
    p.name = "My Collection"
    p.slug = slug
    p.description = "A collection of primitives"
    p.author = _USER_ID.hex
    p.version = "1.0"
    p.tags = []
    p.content_json = {"pins": manifest_pins or []}
    p.source_url = None
    p.forked_from = None
    p.checksum = None
    p.ed25519_signature = None
    p.verified = None
    p.trust_tier = None
    p.tier = "native"
    p.download_count = None
    p.average_rating = None
    p.review_count = None
    p.owner_team_id = None
    p.visibility = "org"
    p.contribution_status = None
    p.auto_update = True
    p.account_id = _USER_ID
    p.version_group_id = None
    p.update_available_version_id = None
    p.status = status
    p.manifest_pins = manifest_pins or []
    p.trust_header = None
    p.created_at = _NOW
    p.updated_at = _NOW
    return p


def _make_collection_pin_primitive(
    slug: str,
    version: str,
    *,
    primitive_type: str = "schema",
) -> MagicMock:
    p = MagicMock()
    p.slug = slug
    p.version = version
    p.primitive_type = primitive_type
    return p


# ---------------------------------------------------------------------------
# PRIMITIVE_TYPES constant
# ---------------------------------------------------------------------------


class TestPrimitiveTypesConstant:
    """Verify the canonical PRIMITIVE_TYPES constant."""

    def test_includes_all_nine_types(self) -> None:
        expected = {
            "schema",
            "agent",
            "workflow",
            "pipeline_template",
            "test_fixture",
            "composite",
            "integration",
            "lifecycle_map",
            "library_collection",
        }
        assert set(PRIMITIVE_TYPES) == expected

    def test_library_collection_included(self) -> None:
        assert "library_collection" in PRIMITIVE_TYPES

    def test_tuple_is_immutable(self) -> None:
        assert isinstance(PRIMITIVE_TYPES, tuple)

    def test_collection_pin_types_is_subset(self) -> None:
        assert COLLECTION_PIN_TYPES.issubset(set(PRIMITIVE_TYPES))

    def test_max_collection_pins_positive(self) -> None:
        assert MAX_COLLECTION_PINS == 25


# ---------------------------------------------------------------------------
# Permission: library.write
# ---------------------------------------------------------------------------


class TestLibraryWritePermission:
    """Verify the library.write permission is registered."""

    def test_library_write_exists(self) -> None:
        assert "library.write" in PERMISSIONS

    def test_library_write_requires_operator(self) -> None:
        assert PERMISSIONS["library.write"] == "operator"


# ---------------------------------------------------------------------------
# Route: POST /collections (create draft)
# ---------------------------------------------------------------------------


class TestCreateCollectionEndpoint:
    def test_create_collection_success(self, client: TestClient) -> None:
        mock_prim = _make_collection_primitive()
        with (
            patch("modulo.api.routes.library.get_primitive_by_slug", new_callable=AsyncMock, return_value=None),
            patch("modulo.api.routes.library.create_library_primitive", new_callable=AsyncMock, return_value=mock_prim),
        ):
            resp = client.post(
                "/api/v1/libraries/collections",
                json={
                    "name": "My Collection",
                    "slug": "my-collection",
                    "manifest_pins": [{"slug": "my-schema", "version": "1.0"}],
                },
            )
        assert resp.status_code == 201
        data = resp.json()
        assert data["status"] == "draft"
        assert data["manifest_pins"] == [{"slug": "my-schema", "version": "1.0"}]

    def test_create_collection_duplicate_slug(self, client: TestClient) -> None:
        existing = _make_collection_primitive()
        with patch("modulo.api.routes.library.get_primitive_by_slug", new_callable=AsyncMock, return_value=existing):
            resp = client.post(
                "/api/v1/libraries/collections",
                json={
                    "name": "My Collection",
                    "slug": "my-collection",
                },
            )
        assert resp.status_code == 409

    def test_create_collection_runner_denied(self, runner_client: TestClient) -> None:
        resp = runner_client.post(
            "/api/v1/libraries/collections",
            json={
                "name": "My Collection",
                "slug": "my-collection",
            },
        )
        assert resp.status_code in (401, 403)


# ---------------------------------------------------------------------------
# Route: PATCH /collections/{id} (update draft)
# ---------------------------------------------------------------------------


class TestUpdateCollectionEndpoint:
    def test_update_collection_success(self, client: TestClient) -> None:
        mock_prim = _make_collection_primitive()
        with (
            patch("modulo.api.routes.library.get_primitive", new_callable=AsyncMock, return_value=mock_prim),
            patch("modulo.api.routes.library.get_primitive_by_slug", new_callable=AsyncMock, return_value=None),
        ):
            resp = client.patch(
                f"/api/v1/libraries/collections/{mock_prim.id}",
                json={
                    "manifest_pins": [{"slug": "new-schema", "version": "2.0"}],
                },
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["manifest_pins"] == [{"slug": "new-schema", "version": "2.0"}]

    def test_update_collection_not_found(self, client: TestClient) -> None:
        with patch("modulo.api.routes.library.get_primitive", new_callable=AsyncMock, return_value=None):
            resp = client.patch(
                f"/api/v1/libraries/collections/{uuid.uuid4()}",
                json={"manifest_pins": []},
            )
        assert resp.status_code == 404

    def test_update_non_collection_type(self, client: TestClient) -> None:
        mock_prim = _make_collection_primitive()
        mock_prim.primitive_type = "workflow"
        with patch("modulo.api.routes.library.get_primitive", new_callable=AsyncMock, return_value=mock_prim):
            resp = client.patch(
                f"/api/v1/libraries/collections/{mock_prim.id}",
                json={"manifest_pins": []},
            )
        assert resp.status_code == 400

    def test_update_published_collection_denied(self, client: TestClient) -> None:
        mock_prim = _make_collection_primitive(status="published")
        with patch("modulo.api.routes.library.get_primitive", new_callable=AsyncMock, return_value=mock_prim):
            resp = client.patch(
                f"/api/v1/libraries/collections/{mock_prim.id}",
                json={"manifest_pins": []},
            )
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Route: POST /collections/{id}/publish
# ---------------------------------------------------------------------------


class TestPublishCollectionEndpoint:
    def test_publish_with_valid_pins(self, client: TestClient) -> None:
        mock_prim = _make_collection_primitive(manifest_pins=[{"slug": "my-schema", "version": "1.0"}])
        pinned = _make_collection_pin_primitive("my-schema", "1.0", primitive_type="schema")
        with (
            patch("modulo.api.routes.library.get_primitive", new_callable=AsyncMock, return_value=mock_prim),
            patch("modulo.api.routes.library._lookup_pin_primitive", new_callable=AsyncMock, return_value=pinned),
        ):
            resp = client.post(
                f"/api/v1/libraries/collections/{mock_prim.id}/publish",
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "published"

    def test_publish_empty_manifest_rejected(self, client: TestClient) -> None:
        mock_prim = _make_collection_primitive(manifest_pins=[])
        with patch("modulo.api.routes.library.get_primitive", new_callable=AsyncMock, return_value=mock_prim):
            resp = client.post(
                f"/api/v1/libraries/collections/{mock_prim.id}/publish",
            )
        assert resp.status_code == 422

    def test_publish_duplicate_pins_rejected(self, client: TestClient) -> None:
        mock_prim = _make_collection_primitive(
            manifest_pins=[
                {"slug": "my-schema", "version": "1.0"},
                {"slug": "my-schema", "version": "1.0"},
            ]
        )
        pinned = _make_collection_pin_primitive("my-schema", "1.0", primitive_type="schema")
        with (
            patch("modulo.api.routes.library.get_primitive", new_callable=AsyncMock, return_value=mock_prim),
            patch("modulo.api.routes.library._lookup_pin_primitive", new_callable=AsyncMock, return_value=pinned),
        ):
            resp = client.post(
                f"/api/v1/libraries/collections/{mock_prim.id}/publish",
            )
        assert resp.status_code == 422

    def test_publish_too_many_pins_rejected(self, client: TestClient) -> None:
        pins = [{"slug": f"schema-{i}", "version": "1.0"} for i in range(26)]
        mock_prim = _make_collection_primitive(manifest_pins=pins)
        with patch("modulo.api.routes.library.get_primitive", new_callable=AsyncMock, return_value=mock_prim):
            resp = client.post(
                f"/api/v1/libraries/collections/{mock_prim.id}/publish",
            )
        assert resp.status_code == 422

    def test_publish_non_draft_denied(self, client: TestClient) -> None:
        mock_prim = _make_collection_primitive(status="published")
        with patch("modulo.api.routes.library.get_primitive", new_callable=AsyncMock, return_value=mock_prim):
            resp = client.post(
                f"/api/v1/libraries/collections/{mock_prim.id}/publish",
            )
        assert resp.status_code == 400

    def test_publish_not_collection_denied(self, client: TestClient) -> None:
        mock_prim = _make_collection_primitive()
        mock_prim.primitive_type = "workflow"
        with patch("modulo.api.routes.library.get_primitive", new_callable=AsyncMock, return_value=mock_prim):
            resp = client.post(
                f"/api/v1/libraries/collections/{mock_prim.id}/publish",
            )
        assert resp.status_code == 400

    def test_publish_runner_denied(self, runner_client: TestClient) -> None:
        mock_prim = _make_collection_primitive(manifest_pins=[{"slug": "my-schema", "version": "1.0"}])
        with patch("modulo.api.routes.library.get_primitive", new_callable=AsyncMock, return_value=mock_prim):
            resp = runner_client.post(
                f"/api/v1/libraries/collections/{mock_prim.id}/publish",
            )
        assert resp.status_code in (401, 403)
