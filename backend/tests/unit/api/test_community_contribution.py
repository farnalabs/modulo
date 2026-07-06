"""API tests for community contribution endpoints."""

import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import ProgrammingError

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.core.library_service import (
    ContributionInvalidTransitionError,
    ContributionNotFoundError,
)
from modulo.settings import Settings, get_settings

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
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


def _make_primitive(
    *,
    pid: uuid.UUID | None = None,
    primitive_type: str = "schema",
    name: str = "Test Schema",
    contribution_status: str | None = None,
    visibility: str = "org",
) -> MagicMock:
    p = MagicMock()
    p.id = pid or uuid.uuid4()
    p.organisation_id = _ORG_ID
    p.source = "local"
    p.primitive_type = primitive_type
    p.name = name
    p.slug = "test-schema"
    p.description = "A test schema"
    p.author = "testuser"
    p.version = "1.0"
    p.tags = []
    p.content_json = {}
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
    p.visibility = visibility
    p.contribution_status = contribution_status
    p.account_id = None
    p.auto_update = True
    p.created_at = _NOW
    p.updated_at = _NOW
    return p


@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="testuser",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
        is_system_admin=True,
    )
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# POST /api/v1/libraries/community/contribute
# ---------------------------------------------------------------------------


class TestCommunityContribute:
    CONTRIBUTE_PATH = "/api/v1/libraries/community/contribute"

    def test_contribute_returns_201(self, client: TestClient) -> None:
        prim = _make_primitive(contribution_status="draft")

        with (
            patch("modulo.api.routes.library.contribute_primitive", new_callable=AsyncMock, return_value=prim),
        ):
            resp = client.post(
                self.CONTRIBUTE_PATH,
                json={
                    "primitive_type": "schema",
                    "name": "Test Schema",
                    "slug": "test-schema",
                    "description": "A test schema",
                    "tags": ["test"],
                    "content_json": {"fields": []},
                },
            )

        assert resp.status_code == 201
        body = resp.json()
        assert body["id"] == str(prim.id)
        assert body["name"] == "Test Schema"

    def test_contribute_returns_422_for_invalid_primitive_type(self, client: TestClient) -> None:
        resp = client.post(
            self.CONTRIBUTE_PATH,
            json={
                "primitive_type": "invalid_type",
                "name": "Test Schema",
                "slug": "test-schema",
                "content_json": {},
            },
        )

        assert resp.status_code == 422

    def test_contribute_returns_422_for_empty_name(self, client: TestClient) -> None:
        resp = client.post(
            self.CONTRIBUTE_PATH,
            json={
                "primitive_type": "schema",
                "name": "",
                "slug": "test-schema",
                "content_json": {},
            },
        )

        assert resp.status_code == 422

    def test_contribute_returns_501_on_programming_error(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.library.contribute_primitive",
                new_callable=AsyncMock,
                side_effect=ProgrammingError("mock", {}, ""),
            ),
        ):
            resp = client.post(
                self.CONTRIBUTE_PATH,
                json={
                    "primitive_type": "schema",
                    "name": "Test Schema",
                    "slug": "test-schema",
                    "content_json": {},
                },
            )

        assert resp.status_code == 501
        assert "migrations" in resp.json()["detail"].lower()

    def test_contribute_accepts_optional_source_url(self, client: TestClient) -> None:
        prim = _make_primitive(contribution_status="draft")

        with (
            patch(
                "modulo.api.routes.library.contribute_primitive",
                new_callable=AsyncMock,
                return_value=prim,
            ) as mock_fn,
        ):
            resp = client.post(
                self.CONTRIBUTE_PATH,
                json={
                    "primitive_type": "workflow",
                    "name": "Test Workflow",
                    "slug": "test-workflow",
                    "description": "A workflow",
                    "tags": ["workflow"],
                    "content_json": {"nodes": [], "edges": []},
                    "source_url": "https://github.com/user/workflow",
                },
            )

        assert resp.status_code == 201
        call_kwargs = mock_fn.call_args.kwargs
        assert call_kwargs["source_url"] == "https://github.com/user/workflow"


# ---------------------------------------------------------------------------
# GET /api/v1/libraries/community/contributions
# ---------------------------------------------------------------------------


class TestListCommunityContributions:
    LIST_PATH = "/api/v1/libraries/community/contributions"

    def test_list_returns_paginated_list(self, client: TestClient) -> None:
        prim = _make_primitive(contribution_status="draft")
        mock_result = MagicMock()
        mock_result.items = [prim]
        mock_result.total = 1
        mock_result.page = 1
        mock_result.page_size = 20

        with (
            patch(
                "modulo.api.routes.library.list_org_contributions",
                new_callable=AsyncMock,
                return_value=mock_result,
            ),
        ):
            resp = client.get(self.LIST_PATH)

        assert resp.status_code == 200
        body = resp.json()
        assert len(body["items"]) == 1
        assert body["total"] == 1
        assert body["page"] == 1
        assert body["page_size"] == 20

    def test_list_filters_by_status(self, client: TestClient) -> None:
        prim = _make_primitive(contribution_status="review_queue")
        mock_result = MagicMock()
        mock_result.items = [prim]
        mock_result.total = 1
        mock_result.page = 1
        mock_result.page_size = 20

        with (
            patch(
                "modulo.api.routes.library.list_org_contributions",
                new_callable=AsyncMock,
                return_value=mock_result,
            ) as mock_fn,
        ):
            resp = client.get(self.LIST_PATH, params={"contribution_status": "review_queue"})

        assert resp.status_code == 200
        call_kwargs = mock_fn.call_args.kwargs
        assert call_kwargs["contribution_status"] == "review_queue"

    def test_list_returns_empty_when_no_contributions(self, client: TestClient) -> None:
        mock_result = MagicMock()
        mock_result.items = []
        mock_result.total = 0
        mock_result.page = 1
        mock_result.page_size = 20

        with (
            patch(
                "modulo.api.routes.library.list_org_contributions",
                new_callable=AsyncMock,
                return_value=mock_result,
            ),
        ):
            resp = client.get(self.LIST_PATH)

        assert resp.status_code == 200
        assert resp.json()["items"] == []
        assert resp.json()["total"] == 0

    def test_list_respects_pagination_params(self, client: TestClient) -> None:
        mock_result = MagicMock()
        mock_result.items = []
        mock_result.total = 0
        mock_result.page = 2
        mock_result.page_size = 5

        with (
            patch(
                "modulo.api.routes.library.list_org_contributions",
                new_callable=AsyncMock,
                return_value=mock_result,
            ) as mock_fn,
        ):
            resp = client.get(self.LIST_PATH, params={"page": 2, "page_size": 5})

        assert resp.status_code == 200
        call_kwargs = mock_fn.call_args.kwargs
        assert call_kwargs["page"] == 2
        assert call_kwargs["page_size"] == 5

    def test_list_returns_501_on_programming_error(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.library.list_org_contributions",
                new_callable=AsyncMock,
                side_effect=ProgrammingError("mock", {}, ""),
            ),
        ):
            resp = client.get(self.LIST_PATH)

        assert resp.status_code == 501
        assert "migrations" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# POST /api/v1/libraries/admin/library/community/publish/{primitive_id}
# ---------------------------------------------------------------------------


class TestAdminPublishContribution:
    def _publish_path(self, prim_id: uuid.UUID) -> str:
        return f"/api/v1/libraries/admin/library/community/publish/{prim_id}"

    def test_publish_returns_200(self, client: TestClient) -> None:
        prim_id = uuid.uuid4()
        published = _make_primitive(pid=prim_id, contribution_status="published", visibility="community")

        with (
            patch("modulo.api.routes.library.publish_contribution", new_callable=AsyncMock, return_value=published),
        ):
            resp = client.post(self._publish_path(prim_id))

        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == str(prim_id)

    def test_publish_returns_404_for_missing(self, client: TestClient) -> None:
        prim_id = uuid.uuid4()

        with (
            patch(
                "modulo.api.routes.library.publish_contribution",
                new_callable=AsyncMock,
                side_effect=ContributionNotFoundError(f"Contribution {prim_id} not found"),
            ),
        ):
            resp = client.post(self._publish_path(prim_id))

        assert resp.status_code == 404
        assert str(prim_id) in resp.json()["detail"]

    def test_publish_returns_400_for_invalid_transition(self, client: TestClient) -> None:
        prim_id = uuid.uuid4()

        with (
            patch(
                "modulo.api.routes.library.publish_contribution",
                new_callable=AsyncMock,
                side_effect=ContributionInvalidTransitionError(
                    f"Cannot publish contribution {prim_id}: expected status 'draft' or 'review_queue', got 'published'"
                ),
            ),
        ):
            resp = client.post(self._publish_path(prim_id))

        assert resp.status_code == 400
        assert "draft" in resp.json()["detail"]

    def test_publish_returns_501_on_programming_error(self, client: TestClient) -> None:
        prim_id = uuid.uuid4()

        with (
            patch(
                "modulo.api.routes.library.publish_contribution",
                new_callable=AsyncMock,
                side_effect=ProgrammingError("mock", {}, ""),
            ),
        ):
            resp = client.post(self._publish_path(prim_id))

        assert resp.status_code == 501
        assert "migrations" in resp.json()["detail"].lower()
