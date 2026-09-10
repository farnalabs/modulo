"""Unit tests for library collection authoring lifecycle (FAR-760).

Unit tier: no DB — CRUD/service functions are patched at the route-module
boundary and the SQLAlchemy session is a contract-correct AsyncMock.
"""

import asyncio
import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.auth.permissions import PERMISSIONS
from modulo.core.feature_flags import FeatureFlagRegistry
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

    # Feature flag registry with library_collection enabled
    mock_registry = FeatureFlagRegistry(current_tier="community")
    mock_registry.set_override("library_collection", True)

    with patch("modulo.api.routes.library.get_registry", return_value=mock_registry):
        test_client = TestClient(app)
    test_client.mock_session = mock_session  # type: ignore[attr-defined]
    return test_client, mock_session


def _build_client_flag(principal: AuthenticatedPrincipal, *, enabled: bool) -> tuple[TestClient, AsyncMock]:
    """Like ``_build_client`` but lets the caller toggle the library_collection flag."""
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

    mock_registry = FeatureFlagRegistry(current_tier="community")
    mock_registry.set_override("library_collection", enabled)

    with patch("modulo.api.routes.library.get_registry", return_value=mock_registry):
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


# ---------------------------------------------------------------------------
# Feature flag gate (library_collection must be enabled)
# ---------------------------------------------------------------------------


class TestCollectionFeatureFlag:
    def test_create_collection_flag_disabled_returns_404(self) -> None:
        client, _ = _build_client_flag(_principal(), enabled=False)
        resp = client.post(
            "/api/v1/libraries/collections",
            json={"name": "My Collection", "slug": "my-collection"},
        )
        assert resp.status_code == 404

    def test_update_collection_flag_disabled_returns_404(self) -> None:
        client, _ = _build_client_flag(_principal(), enabled=False)
        resp = client.patch(
            f"/api/v1/libraries/collections/{uuid.uuid4()}",
            json={"manifest_pins": []},
        )
        assert resp.status_code == 404

    def test_publish_collection_flag_disabled_returns_404(self) -> None:
        client, _ = _build_client_flag(_principal(), enabled=False)
        resp = client.post(f"/api/v1/libraries/collections/{uuid.uuid4()}/publish")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Create collection — DB error paths
# ---------------------------------------------------------------------------


class TestCreateCollectionErrorPaths:
    def test_create_integrity_error_returns_409(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.library.get_primitive_by_slug", new_callable=AsyncMock, return_value=None),
            patch(
                "modulo.api.routes.library.create_library_primitive",
                new_callable=AsyncMock,
                side_effect=IntegrityError("stmt", {}, RuntimeError("conflict")),
            ),
        ):
            resp = client.post(
                "/api/v1/libraries/collections",
                json={"name": "My Collection", "slug": "my-collection"},
            )
        assert resp.status_code == 409

    def test_create_programming_error_returns_501(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.library.get_primitive_by_slug", new_callable=AsyncMock, return_value=None),
            patch(
                "modulo.api.routes.library.create_library_primitive",
                new_callable=AsyncMock,
                side_effect=ProgrammingError("stmt", {}, RuntimeError("missing table")),
            ),
        ):
            resp = client.post(
                "/api/v1/libraries/collections",
                json={"name": "My Collection", "slug": "my-collection"},
            )
        assert resp.status_code == 501

    def test_create_sqlalchemy_error_returns_503(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.library.get_primitive_by_slug", new_callable=AsyncMock, return_value=None),
            patch(
                "modulo.api.routes.library.create_library_primitive",
                new_callable=AsyncMock,
                side_effect=SQLAlchemyError("db down"),
            ),
        ):
            resp = client.post(
                "/api/v1/libraries/collections",
                json={"name": "My Collection", "slug": "my-collection"},
            )
        assert resp.status_code == 503


# ---------------------------------------------------------------------------
# Update collection — DB error paths
# ---------------------------------------------------------------------------


class TestUpdateCollectionErrorPaths:
    def test_update_programming_error_returns_501(self, client: TestClient) -> None:
        with patch(
            "modulo.api.routes.library.get_primitive",
            new_callable=AsyncMock,
            side_effect=ProgrammingError("stmt", {}, RuntimeError("missing table")),
        ):
            resp = client.patch(
                f"/api/v1/libraries/collections/{uuid.uuid4()}",
                json={"manifest_pins": []},
            )
        assert resp.status_code == 501

    def test_update_sqlalchemy_error_returns_503(self, client: TestClient) -> None:
        with patch(
            "modulo.api.routes.library.get_primitive",
            new_callable=AsyncMock,
            side_effect=SQLAlchemyError("db down"),
        ):
            resp = client.patch(
                f"/api/v1/libraries/collections/{uuid.uuid4()}",
                json={"manifest_pins": []},
            )
        assert resp.status_code == 503


# ---------------------------------------------------------------------------
# Publish collection — DB error paths
# ---------------------------------------------------------------------------


class TestPublishCollectionErrorPaths:
    def test_publish_programming_error_returns_501(self, client: TestClient) -> None:
        with patch(
            "modulo.api.routes.library.get_primitive",
            new_callable=AsyncMock,
            side_effect=ProgrammingError("stmt", {}, RuntimeError("missing table")),
        ):
            resp = client.post(f"/api/v1/libraries/collections/{uuid.uuid4()}/publish")
        assert resp.status_code == 501

    def test_publish_sqlalchemy_error_returns_503(self, client: TestClient) -> None:
        with patch(
            "modulo.api.routes.library.get_primitive",
            new_callable=AsyncMock,
            side_effect=SQLAlchemyError("db down"),
        ):
            resp = client.post(f"/api/v1/libraries/collections/{uuid.uuid4()}/publish")
        assert resp.status_code == 503


# ---------------------------------------------------------------------------
# Manifest pin validation (ADR 032)
# ---------------------------------------------------------------------------


class TestValidateManifestPins:
    def test_empty_slug_or_version_rejected(self) -> None:
        from modulo.api.routes.library import _validate_manifest_pins

        async def run() -> list[str]:
            with patch("modulo.api.routes.library._lookup_pin_primitive", new_callable=AsyncMock):
                return await _validate_manifest_pins(
                    [{"slug": "", "version": "1.0"}, {"slug": "s", "version": ""}],
                    MagicMock(),
                    _ORG_ID,
                )

        errors = asyncio.run(run())
        assert len(errors) == 2
        assert any("non-empty slug and version" in e for e in errors)

    def test_unknown_primitive_rejected(self) -> None:
        from modulo.api.routes.library import _validate_manifest_pins

        async def run() -> list[str]:
            with patch(
                "modulo.api.routes.library._lookup_pin_primitive",
                new_callable=AsyncMock,
                return_value=None,
            ):
                return await _validate_manifest_pins([{"slug": "ghost", "version": "1.0"}], MagicMock(), _ORG_ID)

        errors = asyncio.run(run())
        assert any("unknown primitive" in e for e in errors)

    def test_disallowed_pin_type_rejected(self) -> None:
        from modulo.api.routes.library import _validate_manifest_pins

        pinned = _make_collection_pin_primitive("my-composite", "1.0", primitive_type="composite")

        async def run() -> list[str]:
            with patch(
                "modulo.api.routes.library._lookup_pin_primitive",
                new_callable=AsyncMock,
                return_value=pinned,
            ):
                return await _validate_manifest_pins([{"slug": "my-composite", "version": "1.0"}], MagicMock(), _ORG_ID)

        errors = asyncio.run(run())
        assert any("not allowed" in e for e in errors)

    def test_valid_pin_accepted(self) -> None:
        from modulo.api.routes.library import _validate_manifest_pins

        pinned = _make_collection_pin_primitive("my-schema", "1.0", primitive_type="schema")

        async def run() -> list[str]:
            with patch(
                "modulo.api.routes.library._lookup_pin_primitive",
                new_callable=AsyncMock,
                return_value=pinned,
            ):
                return await _validate_manifest_pins([{"slug": "my-schema", "version": "1.0"}], MagicMock(), _ORG_ID)

        errors = asyncio.run(run())
        assert errors == []


# ---------------------------------------------------------------------------
# Route: POST /collections/{id}/install (FAR-762)
# ---------------------------------------------------------------------------


def _make_install_record(
    *,
    install_id: uuid.UUID | None = None,
    collection_id: uuid.UUID | None = None,
    status: str = "installed",
) -> MagicMock:
    rec = MagicMock()
    rec.install_id = install_id or uuid.uuid4()
    rec.collection_id = collection_id or uuid.uuid4()
    rec.collection_version = "1.0"
    rec.organisation_id = _ORG_ID
    rec.status = status
    rec.community_sourced = False
    rec.agents_granted = False
    rec.resolved_manifest = {"schemas": {}, "agents": {}}
    rec.connector_checklist = []
    rec.installed_entities = []
    rec.created_at = _NOW
    return rec


class TestInstallCollectionEndpoint:
    def test_install_success(self, client: TestClient) -> None:
        mock_install = _make_install_record()
        with (
            patch(
                "modulo.api.routes.library.install_collection",
                new_callable=AsyncMock,
                return_value=mock_install,
            ),
            patch(
                "modulo.api.routes.library.compute_runnable",
                new_callable=AsyncMock,
                return_value=True,
            ),
        ):
            resp = client.post(
                f"/api/v1/libraries/collections/{mock_install.collection_id}/install",
            )
        assert resp.status_code == 201
        data = resp.json()
        assert data["status"] == "installed"
        assert data["runnable"] is True
        assert data["install_id"] == str(mock_install.install_id)

    def test_install_not_published(self, client: TestClient) -> None:
        from modulo.core.library_service.install import CollectionNotPublishedError

        coll_id = uuid.uuid4()
        with patch(
            "modulo.api.routes.library.install_collection",
            new_callable=AsyncMock,
            side_effect=CollectionNotPublishedError("not published"),
        ):
            resp = client.post(
                f"/api/v1/libraries/collections/{coll_id}/install",
            )
        assert resp.status_code == 400

    def test_install_pin_resolution_error(self, client: TestClient) -> None:
        from modulo.core.library_service.install import PinResolutionError

        coll_id = uuid.uuid4()
        with patch(
            "modulo.api.routes.library.install_collection",
            new_callable=AsyncMock,
            side_effect=PinResolutionError("pin not found"),
        ):
            resp = client.post(
                f"/api/v1/libraries/collections/{coll_id}/install",
            )
        assert resp.status_code == 422

    def test_install_generic_error(self, client: TestClient) -> None:
        from modulo.core.library_service.install import CollectionInstallError

        coll_id = uuid.uuid4()
        with patch(
            "modulo.api.routes.library.install_collection",
            new_callable=AsyncMock,
            side_effect=CollectionInstallError("something went wrong"),
        ):
            resp = client.post(
                f"/api/v1/libraries/collections/{coll_id}/install",
            )
        assert resp.status_code == 400

    def test_install_runner_denied(self, runner_client: TestClient) -> None:
        coll_id = uuid.uuid4()
        resp = runner_client.post(
            f"/api/v1/libraries/collections/{coll_id}/install",
        )
        assert resp.status_code in (401, 403)

    def test_install_programming_error(self, client: TestClient) -> None:
        coll_id = uuid.uuid4()
        with patch(
            "modulo.api.routes.library.install_collection",
            new_callable=AsyncMock,
            side_effect=ProgrammingError("stmt", {}, RuntimeError("missing table")),
        ):
            resp = client.post(
                f"/api/v1/libraries/collections/{coll_id}/install",
            )
        assert resp.status_code == 501

    def test_install_sqlalchemy_error(self, client: TestClient) -> None:
        coll_id = uuid.uuid4()
        with patch(
            "modulo.api.routes.library.install_collection",
            new_callable=AsyncMock,
            side_effect=SQLAlchemyError("db down"),
        ):
            resp = client.post(
                f"/api/v1/libraries/collections/{coll_id}/install",
            )
        assert resp.status_code == 503


# ---------------------------------------------------------------------------
# Route: POST /collections/{id}/uninstall (FAR-762)
# ---------------------------------------------------------------------------


class TestUninstallCollectionEndpoint:
    def test_uninstall_success(self, client: TestClient) -> None:
        install_id = uuid.uuid4()
        coll_id = uuid.uuid4()
        with patch(
            "modulo.api.routes.library.uninstall_collection",
            new_callable=AsyncMock,
            return_value={
                "install_id": str(install_id),
                "deleted": [],
                "detached": [],
            },
        ):
            resp = client.post(
                f"/api/v1/libraries/collections/{coll_id}/uninstall",
                json={"install_id": str(install_id)},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["install_id"] == str(install_id)
        assert not data["deleted"]
        assert not data["detached"]

    def test_uninstall_not_found(self, client: TestClient) -> None:
        from modulo.core.library_service.uninstall import InstallNotFoundError

        install_id = uuid.uuid4()
        coll_id = uuid.uuid4()
        with patch(
            "modulo.api.routes.library.uninstall_collection",
            new_callable=AsyncMock,
            side_effect=InstallNotFoundError("not found"),
        ):
            resp = client.post(
                f"/api/v1/libraries/collections/{coll_id}/uninstall",
                json={"install_id": str(install_id)},
            )
        assert resp.status_code == 404

    def test_uninstall_generic_error(self, client: TestClient) -> None:
        from modulo.core.library_service.uninstall import UninstallError

        install_id = uuid.uuid4()
        coll_id = uuid.uuid4()
        with patch(
            "modulo.api.routes.library.uninstall_collection",
            new_callable=AsyncMock,
            side_effect=UninstallError("failed"),
        ):
            resp = client.post(
                f"/api/v1/libraries/collections/{coll_id}/uninstall",
                json={"install_id": str(install_id)},
            )
        assert resp.status_code == 400

    def test_uninstall_runner_denied(self, runner_client: TestClient) -> None:
        coll_id = uuid.uuid4()
        resp = runner_client.post(
            f"/api/v1/libraries/collections/{coll_id}/uninstall",
            json={"install_id": str(uuid.uuid4())},
        )
        assert resp.status_code in (401, 403)

    def test_uninstall_programming_error(self, client: TestClient) -> None:
        coll_id = uuid.uuid4()
        with patch(
            "modulo.api.routes.library.uninstall_collection",
            new_callable=AsyncMock,
            side_effect=ProgrammingError("stmt", {}, RuntimeError("missing table")),
        ):
            resp = client.post(
                f"/api/v1/libraries/collections/{coll_id}/uninstall",
                json={"install_id": str(uuid.uuid4())},
            )
        assert resp.status_code == 501

    def test_uninstall_sqlalchemy_error(self, client: TestClient) -> None:
        coll_id = uuid.uuid4()
        with patch(
            "modulo.api.routes.library.uninstall_collection",
            new_callable=AsyncMock,
            side_effect=SQLAlchemyError("db down"),
        ):
            resp = client.post(
                f"/api/v1/libraries/collections/{coll_id}/uninstall",
                json={"install_id": str(uuid.uuid4())},
            )
        assert resp.status_code == 503


# ---------------------------------------------------------------------------
# Route: GET /collections/{id}/installs (FAR-762)
# ---------------------------------------------------------------------------


class TestListCollectionInstallsEndpoint:
    def test_list_installs_programming_error(self, client: TestClient) -> None:
        coll_id = uuid.uuid4()
        with patch(
            "modulo.api.routes.library.select",
            return_value=MagicMock(),
        ):
            client.mock_session.execute = AsyncMock(
                side_effect=ProgrammingError("stmt", {}, RuntimeError("missing table")),
            )
            resp = client.get(
                f"/api/v1/libraries/collections/{coll_id}/installs",
            )
        assert resp.status_code == 501

    def test_list_installs_sqlalchemy_error(self, client: TestClient) -> None:
        coll_id = uuid.uuid4()
        with patch(
            "modulo.api.routes.library.select",
            return_value=MagicMock(),
        ):
            client.mock_session.execute = AsyncMock(
                side_effect=SQLAlchemyError("db down"),
            )
            resp = client.get(
                f"/api/v1/libraries/collections/{coll_id}/installs",
            )
        assert resp.status_code == 503


# ---------------------------------------------------------------------------
# Route: GET /collections/{id}/installs/{install_id} (FAR-762)
# ---------------------------------------------------------------------------


class TestGetCollectionInstallEndpoint:
    def test_get_install_success(self, client: TestClient) -> None:
        mock_install = _make_install_record()
        with patch(
            "modulo.api.routes.library.compute_runnable",
            new_callable=AsyncMock,
            return_value=True,
        ):
            client.mock_session.get = AsyncMock(return_value=mock_install)
            resp = client.get(
                f"/api/v1/libraries/collections/{mock_install.collection_id}/installs/{mock_install.install_id}",
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["install_id"] == str(mock_install.install_id)
        assert data["runnable"] is True

    def test_get_install_not_found(self, client: TestClient) -> None:
        client.mock_session.get = AsyncMock(return_value=None)
        resp = client.get(
            f"/api/v1/libraries/collections/{uuid.uuid4()}/installs/{uuid.uuid4()}",
        )
        assert resp.status_code == 404

    def test_get_install_wrong_collection(self, client: TestClient) -> None:
        mock_install = _make_install_record()
        wrong_collection_id = uuid.uuid4()
        client.mock_session.get = AsyncMock(return_value=mock_install)
        resp = client.get(
            f"/api/v1/libraries/collections/{wrong_collection_id}/installs/{mock_install.install_id}",
        )
        assert resp.status_code == 404

    def test_get_install_programming_error(self, client: TestClient) -> None:
        client.mock_session.get = AsyncMock(
            side_effect=ProgrammingError("stmt", {}, RuntimeError("missing table")),
        )
        resp = client.get(
            f"/api/v1/libraries/collections/{uuid.uuid4()}/installs/{uuid.uuid4()}",
        )
        assert resp.status_code == 501

    def test_get_install_sqlalchemy_error(self, client: TestClient) -> None:
        client.mock_session.get = AsyncMock(
            side_effect=SQLAlchemyError("db down"),
        )
        resp = client.get(
            f"/api/v1/libraries/collections/{uuid.uuid4()}/installs/{uuid.uuid4()}",
        )
        assert resp.status_code == 503


# ---------------------------------------------------------------------------
# Service: install_collection unit tests
# ---------------------------------------------------------------------------


class TestInstallCollectionService:
    def test_install_not_published_raises(self) -> None:
        from modulo.core.library_service.install import (
            CollectionNotPublishedError,
            install_collection,
        )

        async def run() -> None:
            mock_session = AsyncMock()
            mock_collection = MagicMock()
            mock_collection.organisation_id = _ORG_ID
            mock_collection.primitive_type = "library_collection"
            mock_collection.status = "draft"
            mock_session.get = AsyncMock(return_value=mock_collection)
            await install_collection(mock_session, _ORG_ID, _USER_ID, uuid.uuid4())

        with pytest.raises(CollectionNotPublishedError):
            asyncio.run(run())

    def test_install_not_collection_raises(self) -> None:
        from modulo.core.library_service.install import (
            CollectionInstallError,
            install_collection,
        )

        async def run() -> None:
            mock_session = AsyncMock()
            mock_collection = MagicMock()
            mock_collection.organisation_id = _ORG_ID
            mock_collection.primitive_type = "workflow"
            mock_collection.status = "published"
            mock_session.get = AsyncMock(return_value=mock_collection)
            await install_collection(mock_session, _ORG_ID, _USER_ID, uuid.uuid4())

        with pytest.raises(CollectionInstallError, match="not a collection"):
            asyncio.run(run())

    def test_install_empty_pins_raises(self) -> None:
        from modulo.core.library_service.install import (
            CollectionInstallError,
            install_collection,
        )

        async def run() -> None:
            mock_session = AsyncMock()
            mock_collection = MagicMock()
            mock_collection.organisation_id = _ORG_ID
            mock_collection.primitive_type = "library_collection"
            mock_collection.status = "published"
            mock_collection.manifest_pins = []
            mock_session.get = AsyncMock(return_value=mock_collection)
            mock_result = MagicMock()
            mock_result.scalar_one_or_none.return_value = None
            mock_session.execute = AsyncMock(return_value=mock_result)
            await install_collection(mock_session, _ORG_ID, _USER_ID, uuid.uuid4())

        with pytest.raises(CollectionInstallError, match="no manifest pins"):
            asyncio.run(run())


# ---------------------------------------------------------------------------
# Service: grant_collection_agents unit tests (FAR-764)
# ---------------------------------------------------------------------------


class TestGrantCollectionAgents:
    def test_grant_community_sourced(self) -> None:
        from modulo.core.library_service.grant import grant_collection_agents

        async def run() -> None:
            mock_session = AsyncMock()
            mock_install = MagicMock()
            mock_install.organisation_id = _ORG_ID
            mock_install.community_sourced = True
            mock_install.agents_granted = False
            mock_session.get = AsyncMock(return_value=mock_install)
            result = await grant_collection_agents(mock_session, _ORG_ID, mock_install.install_id)
            assert result.agents_granted is True

        asyncio.run(run())

    def test_grant_already_granted(self) -> None:
        from modulo.core.library_service.grant import grant_collection_agents

        async def run() -> None:
            mock_session = AsyncMock()
            mock_install = MagicMock()
            mock_install.organisation_id = _ORG_ID
            mock_install.community_sourced = True
            mock_install.agents_granted = True
            mock_session.get = AsyncMock(return_value=mock_install)
            result = await grant_collection_agents(mock_session, _ORG_ID, mock_install.install_id)
            assert result.agents_granted is True

        asyncio.run(run())

    def test_grant_not_community_sourced(self) -> None:
        from modulo.core.library_service.grant import NotCommunitySourcedError, grant_collection_agents

        async def run() -> None:
            mock_session = AsyncMock()
            mock_install = MagicMock()
            mock_install.organisation_id = _ORG_ID
            mock_install.community_sourced = False
            mock_install.agents_granted = False
            mock_session.get = AsyncMock(return_value=mock_install)
            await grant_collection_agents(mock_session, _ORG_ID, mock_install.install_id)

        with pytest.raises(NotCommunitySourcedError):
            asyncio.run(run())

    def test_grant_install_not_found(self) -> None:
        from modulo.core.library_service.grant import InstallNotFoundError, grant_collection_agents

        async def run() -> None:
            mock_session = AsyncMock()
            mock_session.get = AsyncMock(return_value=None)
            await grant_collection_agents(mock_session, _ORG_ID, uuid.uuid4())

        with pytest.raises(InstallNotFoundError):
            asyncio.run(run())

    def test_grant_wrong_org(self) -> None:
        from modulo.core.library_service.grant import InstallNotFoundError, grant_collection_agents

        async def run() -> None:
            mock_session = AsyncMock()
            mock_install = MagicMock()
            mock_install.organisation_id = uuid.uuid4()  # different org
            mock_session.get = AsyncMock(return_value=mock_install)
            await grant_collection_agents(mock_session, _ORG_ID, mock_install.install_id)

        with pytest.raises(InstallNotFoundError):
            asyncio.run(run())


# ---------------------------------------------------------------------------
# API: grant endpoint tests (FAR-764)
# ---------------------------------------------------------------------------


def _make_install_record_with_grant(
    *,
    install_id: uuid.UUID | None = None,
    community_sourced: bool = True,
    agents_granted: bool = False,
) -> MagicMock:
    rec = _make_install_record(install_id=install_id)
    rec.community_sourced = community_sourced
    rec.agents_granted = agents_granted
    return rec


class TestGrantCollectionAgentsEndpoint:
    def test_grant_success(self, client: TestClient) -> None:
        mock_install = _make_install_record_with_grant()
        with (
            patch(
                "modulo.api.routes.library.grant_collection_agents",
                new_callable=AsyncMock,
                return_value=mock_install,
            ),
            patch(
                "modulo.api.routes.library.compute_runnable",
                new_callable=AsyncMock,
                return_value=True,
            ),
        ):
            resp = client.post(
                f"/api/v1/libraries/collections/{mock_install.collection_id}/installs/{mock_install.install_id}/grant",
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["community_sourced"] is True
        assert data["agents_granted"] is False

    def test_grant_not_found(self, client: TestClient) -> None:
        from modulo.core.library_service.grant import InstallNotFoundError

        coll_id = uuid.uuid4()
        install_id = uuid.uuid4()
        with patch(
            "modulo.api.routes.library.grant_collection_agents",
            new_callable=AsyncMock,
            side_effect=InstallNotFoundError("not found"),
        ):
            resp = client.post(
                f"/api/v1/libraries/collections/{coll_id}/installs/{install_id}/grant",
            )
        assert resp.status_code == 404

    def test_grant_not_community(self, client: TestClient) -> None:
        from modulo.core.library_service.grant import NotCommunitySourcedError

        coll_id = uuid.uuid4()
        install_id = uuid.uuid4()
        with patch(
            "modulo.api.routes.library.grant_collection_agents",
            new_callable=AsyncMock,
            side_effect=NotCommunitySourcedError("not community"),
        ):
            resp = client.post(
                f"/api/v1/libraries/collections/{coll_id}/installs/{install_id}/grant",
            )
        assert resp.status_code == 400

    def test_grant_programming_error(self, client: TestClient) -> None:
        coll_id = uuid.uuid4()
        install_id = uuid.uuid4()
        with patch(
            "modulo.api.routes.library.grant_collection_agents",
            new_callable=AsyncMock,
            side_effect=ProgrammingError("stmt", {}, RuntimeError("missing table")),
        ):
            resp = client.post(
                f"/api/v1/libraries/collections/{coll_id}/installs/{install_id}/grant",
            )
        assert resp.status_code == 501

    def test_grant_sqlalchemy_error(self, client: TestClient) -> None:
        coll_id = uuid.uuid4()
        install_id = uuid.uuid4()
        with patch(
            "modulo.api.routes.library.grant_collection_agents",
            new_callable=AsyncMock,
            side_effect=SQLAlchemyError("db down"),
        ):
            resp = client.post(
                f"/api/v1/libraries/collections/{coll_id}/installs/{install_id}/grant",
            )
        assert resp.status_code == 503

    def test_install_response_includes_grant_fields(self, client: TestClient) -> None:
        mock_install = _make_install_record_with_grant(community_sourced=True, agents_granted=True)
        with (
            patch(
                "modulo.api.routes.library.install_collection",
                new_callable=AsyncMock,
                return_value=mock_install,
            ),
            patch(
                "modulo.api.routes.library.compute_runnable",
                new_callable=AsyncMock,
                return_value=True,
            ),
        ):
            resp = client.post(
                f"/api/v1/libraries/collections/{mock_install.collection_id}/install",
            )
        assert resp.status_code == 201
        data = resp.json()
        assert "community_sourced" in data
        assert "agents_granted" in data
        assert data["community_sourced"] is True
        assert data["agents_granted"] is True


class TestUninstallCollectionService:
    def test_uninstall_collection_id_mismatch_raises(self) -> None:
        from modulo.core.library_service.uninstall import (
            InstallNotFoundError,
            uninstall_collection,
        )

        async def run() -> None:
            mock_session = AsyncMock()
            install = MagicMock()
            install.organisation_id = _ORG_ID
            install.collection_id = uuid.uuid4()
            mock_session.get = AsyncMock(return_value=install)
            await uninstall_collection(
                mock_session,
                _ORG_ID,
                uuid.uuid4(),
                collection_id=uuid.uuid4(),
            )

        with pytest.raises(InstallNotFoundError):
            asyncio.run(run())

    def test_uninstall_skips_missing_entity(self) -> None:
        from modulo.core.library_service.uninstall import uninstall_collection

        async def run() -> dict:
            mock_session = AsyncMock()
            install = MagicMock()
            install.organisation_id = _ORG_ID
            install.collection_id = uuid.uuid4()
            mock_session.get = AsyncMock(return_value=install)

            # Tracking row points at an entity that no longer exists.
            row = MagicMock()
            row.entity_type = "schema"
            row.entity_id = uuid.uuid4()
            entity_result = MagicMock()
            entity_result.scalars = MagicMock(return_value=[row])
            mock_session.execute = AsyncMock(return_value=entity_result)
            # _entity_exists → False (entity row gone)
            mock_session.scalar = AsyncMock(return_value=None)

            return await uninstall_collection(mock_session, _ORG_ID, uuid.uuid4())

        result = asyncio.run(run())
        assert not result["deleted"]
        assert not result["detached"]

    def test_check_unmodified_true_when_stamped(self) -> None:
        from modulo.core.library_service.uninstall import _check_unmodified

        async def run() -> bool:
            install_id = uuid.uuid4()
            entity = MagicMock()
            entity.collection_install_id = install_id
            mock_session = AsyncMock()
            mock_session.scalar = AsyncMock(return_value=entity)
            return await _check_unmodified(mock_session, "schema", uuid.uuid4(), install_id)

        assert asyncio.run(run()) is True

    def test_check_unmodified_false_when_detached(self) -> None:
        from modulo.core.library_service.uninstall import _check_unmodified

        async def run() -> bool:
            install_id = uuid.uuid4()
            entity = MagicMock()
            entity.collection_install_id = uuid.uuid4()  # different stamp
            mock_session = AsyncMock()
            mock_session.scalar = AsyncMock(return_value=entity)
            return await _check_unmodified(mock_session, "schema", uuid.uuid4(), install_id)

        assert asyncio.run(run()) is False
