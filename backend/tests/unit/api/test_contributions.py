"""Unit tests for /api/v1/library/contribute endpoints."""

import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_tenant_user, get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal, TenantPrincipal
from modulo.core.library_service import (
    ContributionInvalidTransitionError,
    ContributionNotFoundError,
)
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_PRIMITIVE_ID = uuid.uuid4()


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
    authz_result = MagicMock()
    authz_result.scalar_one_or_none = MagicMock(return_value=True)
    session.execute = AsyncMock(return_value=authz_result)
    return session


def _make_mock_primitive(**overrides) -> MagicMock:
    p = MagicMock()
    p.id = overrides.get("id", _PRIMITIVE_ID)
    p.organisation_id = overrides.get("organisation_id", _ORG_ID)
    p.source = "local"
    p.primitive_type = "test_fixture"
    p.contribution_status = overrides.get("contribution_status", "draft")
    p.visibility = overrides.get("visibility", "org")
    p.name = overrides.get("name", "Test Fixture")
    p.slug = overrides.get("slug", "test-fixture")
    p.description = "A test fixture"
    p.author = "tester"
    p.version = "1.0"
    p.tags = ["test"]
    p.content_json = {}
    p.source_url = None
    p.forked_from = None
    p.checksum = None
    p.ed25519_signature = None
    p.verified = None
    p.trust_tier = "unverified"
    p.tier = "community"
    p.download_count = None
    p.average_rating = None
    p.review_count = None
    p.owner_team_id = None
    p.created_by = None
    p.account_id = uuid.UUID("00000000-0000-0000-0000-000000000002")
    p.created_at = datetime(2025, 1, 1, tzinfo=UTC)
    p.updated_at = datetime(2025, 1, 1, tzinfo=UTC)
    for k, v in overrides.items():
        setattr(p, k, v)
    return p


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="testuser",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )
    app.dependency_overrides[get_current_tenant_user] = lambda: TenantPrincipal(
        username="tenant", organisation_id=_ORG_ID, account_id=_USER_ID, org_role="admin"
    )
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def viewer_client() -> Generator[TestClient, None, None]:
    session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="viewer",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="viewer",
    )
    app.dependency_overrides[get_current_tenant_user] = lambda: TenantPrincipal(
        username="tenant", organisation_id=_ORG_ID, account_id=_USER_ID, org_role="viewer"
    )
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


class TestCreateContribution:
    def test_create_contribution_returns_201(self, client: TestClient):
        prim = _make_mock_primitive()

        with (
            patch(
                "modulo.api.routes.contributions.contribute_fixture",
                new_callable=AsyncMock,
                return_value=prim,
            ),
            patch("modulo.api.routes.contributions.set_rls_org", new_callable=AsyncMock),
        ):
            resp = client.post(
                "/api/v1/library/contribute",
                json={
                    "name": "My Fixture",
                    "slug": "my-fixture",
                    "description": "A test fixture",
                    "tags": ["test", "fixture"],
                    "fixture_map": {"input": "output"},
                },
            )

        assert resp.status_code == 201
        body = resp.json()
        assert body["id"] == str(_PRIMITIVE_ID)
        assert body["contribution_status"] == "draft"
        assert body["name"] == "Test Fixture"
        assert body["slug"] == "test-fixture"

    def test_create_contribution_minimal_fields(self, client: TestClient):
        prim = _make_mock_primitive()

        with (
            patch(
                "modulo.api.routes.contributions.contribute_fixture",
                new_callable=AsyncMock,
                return_value=prim,
            ),
            patch("modulo.api.routes.contributions.set_rls_org", new_callable=AsyncMock),
        ):
            resp = client.post(
                "/api/v1/library/contribute",
                json={
                    "name": "Minimal",
                    "slug": "minimal",
                    "fixture_map": {"a": "b"},
                },
            )

        assert resp.status_code == 201

    def test_create_contribution_missing_name_returns_422(self, client: TestClient):
        resp = client.post(
            "/api/v1/library/contribute",
            json={
                "slug": "my-fixture",
                "fixture_map": {"input": "output"},
            },
        )
        assert resp.status_code == 422

    def test_create_contribution_missing_fixture_map_returns_422(self, client: TestClient):
        resp = client.post(
            "/api/v1/library/contribute",
            json={
                "name": "My Fixture",
                "slug": "my-fixture",
            },
        )
        assert resp.status_code == 422

    def test_create_contribution_passes_correct_params(self, client: TestClient):
        prim = _make_mock_primitive()
        run_id = uuid.uuid4()
        pipeline_id = uuid.uuid4()
        team_id = uuid.uuid4()

        with (
            patch(
                "modulo.api.routes.contributions.contribute_fixture",
                new_callable=AsyncMock,
                return_value=prim,
            ) as mock_contribute,
            patch("modulo.api.routes.contributions.set_rls_org", new_callable=AsyncMock),
        ):
            resp = client.post(
                "/api/v1/library/contribute",
                json={
                    "name": "With Refs",
                    "slug": "with-refs",
                    "description": "A fixture with references",
                    "tags": ["test"],
                    "fixture_map": {"prompt": "response"},
                    "source_run_id": str(run_id),
                    "source_pipeline_id": str(pipeline_id),
                    "owner_team_id": str(team_id),
                },
            )

        assert resp.status_code == 201
        mock_contribute.assert_awaited_once()
        call_kwargs = mock_contribute.call_args.kwargs
        assert call_kwargs["name"] == "With Refs"
        assert call_kwargs["fixture_map"] == {"prompt": "response"}
        assert call_kwargs["source_run_id"] == run_id
        assert call_kwargs["source_pipeline_id"] == pipeline_id
        assert call_kwargs["owner_team_id"] == team_id
        assert call_kwargs["created_by"] == _USER_ID


class TestSubmitForReview:
    def test_submit_for_review_returns_200(self, client: TestClient):
        prim = _make_mock_primitive(contribution_status="review_queue")

        with (
            patch(
                "modulo.api.routes.contributions.submit_contribution_for_review",
                new_callable=AsyncMock,
                return_value=prim,
            ),
            patch("modulo.api.routes.contributions.set_rls_org", new_callable=AsyncMock),
        ):
            resp = client.post(f"/api/v1/library/contribute/{_PRIMITIVE_ID}/submit")

        assert resp.status_code == 200
        body = resp.json()
        assert body["contribution_status"] == "review_queue"
        assert body["visibility"] == "org"

    def test_submit_for_review_uses_created_by_kwarg(self, client: TestClient):
        prim = _make_mock_primitive(contribution_status="review_queue")

        with (
            patch(
                "modulo.api.routes.contributions.submit_contribution_for_review",
                new_callable=AsyncMock,
                return_value=prim,
            ) as mock_submit,
            patch("modulo.api.routes.contributions.set_rls_org", new_callable=AsyncMock),
        ):
            resp = client.post(f"/api/v1/library/contribute/{_PRIMITIVE_ID}/submit")

        assert resp.status_code == 200
        mock_submit.assert_awaited_once()
        assert mock_submit.call_args.kwargs["_created_by"] == _USER_ID

    def test_submit_for_review_404(self, client: TestClient):
        with (
            patch(
                "modulo.api.routes.contributions.submit_contribution_for_review",
                new_callable=AsyncMock,
                side_effect=ContributionNotFoundError("not found"),
            ),
            patch("modulo.api.routes.contributions.set_rls_org", new_callable=AsyncMock),
        ):
            resp = client.post(f"/api/v1/library/contribute/{uuid.uuid4()}/submit")

        assert resp.status_code == 404

    def test_submit_for_review_409(self, client: TestClient):
        with (
            patch(
                "modulo.api.routes.contributions.submit_contribution_for_review",
                new_callable=AsyncMock,
                side_effect=ContributionInvalidTransitionError("already published"),
            ),
            patch("modulo.api.routes.contributions.set_rls_org", new_callable=AsyncMock),
        ):
            resp = client.post(f"/api/v1/library/contribute/{_PRIMITIVE_ID}/submit")

        assert resp.status_code == 409


class TestPublish:
    def test_publish_contribution_returns_200(self, client: TestClient):
        prim = _make_mock_primitive(contribution_status="published", visibility="community")

        with (
            patch(
                "modulo.api.routes.contributions.publish_contribution",
                new_callable=AsyncMock,
                return_value=prim,
            ),
            patch("modulo.api.routes.contributions.set_rls_org", new_callable=AsyncMock),
        ):
            resp = client.post(f"/api/v1/library/contribute/{_PRIMITIVE_ID}/publish")

        assert resp.status_code == 200
        body = resp.json()
        assert body["contribution_status"] == "published"
        assert body["visibility"] == "community"

    def test_publish_contribution_forbidden_for_viewer(self, viewer_client: TestClient):
        resp = viewer_client.post(f"/api/v1/library/contribute/{_PRIMITIVE_ID}/publish")
        assert resp.status_code == 403

    def test_publish_contribution_404(self, client: TestClient):
        with (
            patch(
                "modulo.api.routes.contributions.publish_contribution",
                new_callable=AsyncMock,
                side_effect=ContributionNotFoundError("not found"),
            ),
            patch("modulo.api.routes.contributions.set_rls_org", new_callable=AsyncMock),
        ):
            resp = client.post(f"/api/v1/library/contribute/{uuid.uuid4()}/publish")

        assert resp.status_code == 404

    def test_publish_contribution_409(self, client: TestClient):
        with (
            patch(
                "modulo.api.routes.contributions.publish_contribution",
                new_callable=AsyncMock,
                side_effect=ContributionInvalidTransitionError("not in review_queue"),
            ),
            patch("modulo.api.routes.contributions.set_rls_org", new_callable=AsyncMock),
        ):
            resp = client.post(f"/api/v1/library/contribute/{_PRIMITIVE_ID}/publish")

        assert resp.status_code == 409


class TestListContributions:
    def test_list_contributions_returns_200(self, client: TestClient):
        prim = _make_mock_primitive()
        page_result = MagicMock()
        page_result.items = [prim]
        page_result.total = 1
        page_result.page = 1
        page_result.page_size = 20

        with (
            patch(
                "modulo.api.routes.contributions.list_contributions",
                new_callable=AsyncMock,
                return_value=page_result,
            ),
            patch("modulo.api.routes.contributions.set_rls_org", new_callable=AsyncMock),
        ):
            resp = client.get("/api/v1/library/contribute")

        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert len(body["items"]) == 1

    def test_list_contributions_empty(self, client: TestClient):
        page_result = MagicMock()
        page_result.items = []
        page_result.total = 0
        page_result.page = 1
        page_result.page_size = 20

        with (
            patch(
                "modulo.api.routes.contributions.list_contributions",
                new_callable=AsyncMock,
                return_value=page_result,
            ),
            patch("modulo.api.routes.contributions.set_rls_org", new_callable=AsyncMock),
        ):
            resp = client.get("/api/v1/library/contribute?page=1&page_size=10")

        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 0
        assert not body["items"]

    def test_list_contributions_with_status_filter(self, client: TestClient):
        """Verify the contribution_status query param is accepted."""
        page_result = MagicMock()
        page_result.items = []
        page_result.total = 0
        page_result.page = 1
        page_result.page_size = 20

        with (
            patch(
                "modulo.api.routes.contributions.list_contributions",
                new_callable=AsyncMock,
                return_value=page_result,
            ) as mock_list,
            patch("modulo.api.routes.contributions.set_rls_org", new_callable=AsyncMock),
        ):
            resp = client.get("/api/v1/library/contribute?contribution_status=draft")

        assert resp.status_code == 200
        mock_list.assert_awaited_once()
        assert mock_list.call_args.kwargs["contribution_status"] == "draft"


class TestSubmitVersion:
    def test_submit_version_returns_201(self, client: TestClient):
        prim = _make_mock_primitive(version="1.1", contribution_status="draft")

        with (
            patch(
                "modulo.api.routes.contributions.submit_contribution_version",
                new_callable=AsyncMock,
                return_value=prim,
            ),
            patch("modulo.api.routes.contributions.set_rls_org", new_callable=AsyncMock),
        ):
            resp = client.post(
                f"/api/v1/library/contribute/{_PRIMITIVE_ID}/versions",
                json={
                    "name": "My Fixture v2",
                    "slug": "my-fixture",
                    "description": "Updated fixture",
                    "tags": ["test", "v2"],
                    "fixture_map": {"input": "output_v2"},
                },
            )

        assert resp.status_code == 201
        body = resp.json()
        assert body["name"] == "Test Fixture"
        assert body["contribution_status"] == "draft"

    def test_submit_version_uses_created_by_kwarg(self, client: TestClient):
        prim = _make_mock_primitive(version="1.1", contribution_status="draft")

        with (
            patch(
                "modulo.api.routes.contributions.submit_contribution_version",
                new_callable=AsyncMock,
                return_value=prim,
            ) as mock_version,
            patch("modulo.api.routes.contributions.set_rls_org", new_callable=AsyncMock),
        ):
            resp = client.post(
                f"/api/v1/library/contribute/{_PRIMITIVE_ID}/versions",
                json={
                    "name": "Versioned Fixture",
                    "slug": "versioned-fixture",
                    "fixture_map": {"prompt": "response"},
                },
            )

        assert resp.status_code == 201
        mock_version.assert_awaited_once()
        assert mock_version.call_args.kwargs["created_by"] == _USER_ID

    def test_submit_version_404(self, client: TestClient):
        with (
            patch(
                "modulo.api.routes.contributions.submit_contribution_version",
                new_callable=AsyncMock,
                side_effect=ContributionNotFoundError("not found"),
            ),
            patch("modulo.api.routes.contributions.set_rls_org", new_callable=AsyncMock),
        ):
            resp = client.post(
                f"/api/v1/library/contribute/{uuid.uuid4()}/versions",
                json={
                    "name": "Nope",
                    "slug": "nope",
                    "fixture_map": {"a": "b"},
                },
            )

        assert resp.status_code == 404

    def test_submit_version_409_for_draft_original(self, client: TestClient):
        with (
            patch(
                "modulo.api.routes.contributions.submit_contribution_version",
                new_callable=AsyncMock,
                side_effect=ContributionInvalidTransitionError("expected 'published', got 'draft'"),
            ),
            patch("modulo.api.routes.contributions.set_rls_org", new_callable=AsyncMock),
        ):
            resp = client.post(
                f"/api/v1/library/contribute/{_PRIMITIVE_ID}/versions",
                json={
                    "name": "Nope",
                    "slug": "nope",
                    "fixture_map": {"a": "b"},
                },
            )

        assert resp.status_code == 409

    def test_submit_version_passes_correct_params(self, client: TestClient):
        prim = _make_mock_primitive(version="1.1", contribution_status="draft")

        with (
            patch(
                "modulo.api.routes.contributions.submit_contribution_version",
                new_callable=AsyncMock,
                return_value=prim,
            ) as mock_version,
            patch("modulo.api.routes.contributions.set_rls_org", new_callable=AsyncMock),
        ):
            resp = client.post(
                f"/api/v1/library/contribute/{_PRIMITIVE_ID}/versions",
                json={
                    "name": "Versioned Fixture",
                    "slug": "versioned-fixture",
                    "description": "A versioned fixture",
                    "tags": ["test"],
                    "fixture_map": {"prompt": "response"},
                    "source_run_id": str(uuid.uuid4()),
                    "source_pipeline_id": str(uuid.uuid4()),
                },
            )

        assert resp.status_code == 201
        mock_version.assert_awaited_once()
        assert mock_version.call_args.args[2] == _PRIMITIVE_ID
        assert mock_version.call_args.kwargs["name"] == "Versioned Fixture"
        assert mock_version.call_args.kwargs["fixture_map"] == {"prompt": "response"}


class TestListVersions:
    def test_list_versions_returns_200(self, client: TestClient):
        v1 = _make_mock_primitive(version="1.0", contribution_status="published")
        v2 = _make_mock_primitive(version="1.1", contribution_status="draft", id=uuid.uuid4())
        versions = [v2, v1]

        with (
            patch(
                "modulo.api.routes.contributions.list_contribution_versions",
                new_callable=AsyncMock,
                return_value=versions,
            ),
            patch("modulo.api.routes.contributions.set_rls_org", new_callable=AsyncMock),
        ):
            resp = client.get(f"/api/v1/library/contribute/{_PRIMITIVE_ID}/versions")

        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 2
        assert len(body["versions"]) == 2

    def test_list_versions_empty(self, client: TestClient):
        prim = _make_mock_primitive(version="1.0", contribution_status="published")

        with (
            patch(
                "modulo.api.routes.contributions.list_contribution_versions",
                new_callable=AsyncMock,
                return_value=[prim],
            ),
            patch("modulo.api.routes.contributions.set_rls_org", new_callable=AsyncMock),
        ):
            resp = client.get(f"/api/v1/library/contribute/{_PRIMITIVE_ID}/versions")

        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert body["versions"][0]["version"] == "1.0"

    def test_list_versions_404(self, client: TestClient):
        with (
            patch(
                "modulo.api.routes.contributions.list_contribution_versions",
                new_callable=AsyncMock,
                side_effect=ContributionNotFoundError("not found"),
            ),
            patch("modulo.api.routes.contributions.set_rls_org", new_callable=AsyncMock),
        ):
            resp = client.get(f"/api/v1/library/contribute/{uuid.uuid4()}/versions")

        assert resp.status_code == 404
