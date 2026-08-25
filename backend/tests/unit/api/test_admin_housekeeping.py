"""Unit tests for /api/v1/admin/housekeeping endpoints."""

import uuid
from collections.abc import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError

from modulo.api.dependencies import get_db_session, get_plan_context
from modulo.api.main import app
from modulo.api.routes.admin_housekeeping import CleanupResponse
from modulo.auth.dependencies import get_current_tenant_user
from modulo.auth.jwt import TenantPrincipal
from modulo.core.housekeeping import Candidate, CategoryResult
from modulo.settings import Settings, get_settings

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")


def _make_principal(role: str = "admin") -> TenantPrincipal:
    return TenantPrincipal(
        username="admin" if role == "admin" else "viewer",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role=role,
    )


def _make_candidate(*, entity_type: str = "secret") -> Candidate:
    return Candidate(
        id=str(uuid.uuid4()),
        name="stale-key",
        detail="Orphan secret",
        created_at="2026-01-01T00:00:00+00:00",
        entity_type=entity_type,
    )


def _make_scan_response(category: str = "orphan_secrets", entity_type: str = "secret") -> CategoryResult:
    return CategoryResult(category=category, candidates=[_make_candidate(entity_type=entity_type)])


def _authz_result() -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=True)
    return result


def _make_mock_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    session.execute = AsyncMock(return_value=_authz_result())
    return session


@pytest.fixture
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
    app.dependency_overrides[get_current_tenant_user] = lambda: _make_principal("admin")
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def viewer_client() -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = lambda: Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key="a" * 32,
        fernet_key="a" * 32,
        modulo_admin_password="testpass",
    )
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_current_tenant_user] = lambda: _make_principal("viewer")
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def unauth_client() -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_settings] = lambda: Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key="a" * 32,
        fernet_key="a" * 32,
        modulo_admin_password="testpass",
    )

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield _make_mock_session()

    app.dependency_overrides[get_db_session] = override_session

    async def raise_unauthorized() -> None:
        from fastapi import HTTPException, status

        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

    app.dependency_overrides[get_current_tenant_user] = raise_unauthorized
    yield TestClient(app)
    app.dependency_overrides.clear()


class TestListHousekeeping:
    URL = "/api/v1/admin/housekeeping"

    def test_returns_200_with_categories(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.admin_housekeeping.scan_all", return_value=[_make_scan_response()]),
            patch("modulo.api.routes.admin_housekeeping.set_rls_org"),
        ):
            resp = client.get(self.URL)
        assert resp.status_code == 200
        body = resp.json()
        assert body["total_count"] == 1
        assert len(body["categories"]) == 1
        cat = body["categories"][0]
        assert cat["category"] == "orphan_secrets"
        assert cat["count"] == 1
        candidate = cat["candidates"][0]
        assert candidate["entity_type"] == "secret"
        assert set(candidate.keys()) == {"id", "name", "detail", "created_at", "entity_type"}

    def test_returns_200_when_no_candidates(self, client: TestClient) -> None:
        with (
            patch("modulo.api.routes.admin_housekeeping.scan_all", return_value=[]),
            patch("modulo.api.routes.admin_housekeeping.set_rls_org"),
        ):
            resp = client.get(self.URL)
        assert resp.status_code == 200
        body = resp.json()
        assert body["total_count"] == 0
        assert not body["categories"]

    def test_returns_403_for_non_admin(self, viewer_client: TestClient) -> None:
        resp = viewer_client.get(self.URL)
        assert resp.status_code == 403

    def test_returns_401_when_unauthenticated(self, unauth_client: TestClient) -> None:
        resp = unauth_client.get(self.URL)
        assert resp.status_code == 401

    def test_returns_501_on_programming_error(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.admin_housekeeping.scan_all",
                side_effect=ProgrammingError("stmt", {}, Exception("missing table")),
            ),
            patch("modulo.api.routes.admin_housekeeping.set_rls_org"),
        ):
            resp = client.get(self.URL)
        assert resp.status_code == 501

    def test_returns_503_on_sqlalchemy_error(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.admin_housekeeping.scan_all",
                side_effect=SQLAlchemyError("connection lost"),
            ),
            patch("modulo.api.routes.admin_housekeeping.set_rls_org"),
        ):
            resp = client.get(self.URL)
        assert resp.status_code == 503

    def test_returns_500_on_unexpected_error(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.admin_housekeeping.scan_all",
                side_effect=RuntimeError("boom"),
            ),
            patch("modulo.api.routes.admin_housekeeping.set_rls_org"),
        ):
            resp = client.get(self.URL)
        assert resp.status_code == 500


class TestPerformCleanup:
    URL = "/api/v1/admin/housekeeping/cleanup"

    def test_deletes_selected_items(self, client: TestClient) -> None:
        target_id = str(uuid.uuid4())
        with patch("modulo.api.routes.admin_housekeeping.set_rls_org"):
            session = _make_mock_session()
            session.begin_nested = MagicMock(return_value=_make_begin_nested())
            session.execute = AsyncMock()
            obj = MagicMock()
            session.execute.return_value.scalar_one_or_none = MagicMock(return_value=obj)

            async def override_session() -> AsyncGenerator[AsyncMock, None]:
                yield session

            client.app.dependency_overrides[get_db_session] = override_session
            resp = client.post(self.URL, json={"items": [{"id": target_id, "entity_type": "secret"}]})

        assert resp.status_code == 200
        body = resp.json()
        assert body["deleted_count"] == 1
        assert not body["errors"]
        session.delete.assert_awaited_once_with(obj)

    def test_unknown_entity_type_reports_error(self, client: TestClient) -> None:
        with patch("modulo.api.routes.admin_housekeeping.set_rls_org"):
            session = _make_mock_session()
            session.begin_nested = MagicMock(return_value=_make_begin_nested())

            async def override_session() -> AsyncGenerator[AsyncMock, None]:
                yield session

            client.app.dependency_overrides[get_db_session] = override_session
            resp = client.post(
                self.URL,
                json={"items": [{"id": str(uuid.uuid4()), "entity_type": "does_not_exist"}]},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["deleted_count"] == 0
        assert body["errors"] == [{"entity_type": "does_not_exist", "error": "Unknown entity type: does_not_exist"}]

    def test_invalid_org_fk_is_triage_only(self, client: TestClient) -> None:
        with patch("modulo.api.routes.admin_housekeeping.set_rls_org"):
            session = _make_mock_session()

            async def override_session() -> AsyncGenerator[AsyncMock, None]:
                yield session

            client.app.dependency_overrides[get_db_session] = override_session
            resp = client.post(
                self.URL,
                json={"items": [{"id": str(uuid.uuid4()), "entity_type": "invalid_org_fk"}]},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["deleted_count"] == 0
        assert body["errors"] == [
            {"entity_type": "invalid_org_fk", "error": "Surfaced for triage only — not auto-deleted."}
        ]
        # The triage-only type must never reach the delete path.
        session.delete.assert_not_awaited()

    def test_integrity_error_does_not_block_other_types(self, client: TestClient) -> None:
        with patch("modulo.api.routes.admin_housekeeping.set_rls_org"):
            session = _make_mock_session()

            first = MagicMock()
            first.scalar_one_or_none = MagicMock(return_value=MagicMock())
            second = MagicMock()
            second.scalar_one_or_none = MagicMock(return_value=MagicMock())

            begin_nested_calls = [_make_begin_nested(raise_on_enter=False), _make_begin_nested()]

            session.begin_nested = MagicMock(side_effect=begin_nested_calls)
            authz_result = MagicMock()
            authz_result.scalar_one_or_none = MagicMock(return_value=True)
            session.execute = AsyncMock(side_effect=[authz_result, first, second])
            session.delete = AsyncMock(side_effect=[IntegrityError("stmt", {}, Exception("fk violation")), None])

            async def override_session() -> AsyncGenerator[AsyncMock, None]:
                yield session

            client.app.dependency_overrides[get_db_session] = override_session
            resp = client.post(
                self.URL,
                json={
                    "items": [
                        {"id": str(uuid.uuid4()), "entity_type": "secret"},
                        {"id": str(uuid.uuid4()), "entity_type": "connector"},
                    ]
                },
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["deleted_count"] == 1
        assert len(body["errors"]) == 1
        assert body["errors"][0]["entity_type"] == "secret"
        assert body["errors"][0]["error"] == "Foreign key constraint violation"
        assert body["errors"][0]["id"]

    def test_returns_403_for_non_admin(self, viewer_client: TestClient) -> None:
        resp = viewer_client.post(self.URL, json={"items": []})
        assert resp.status_code == 403

    def test_returns_401_when_unauthenticated(self, unauth_client: TestClient) -> None:
        resp = unauth_client.post(self.URL, json={"items": []})
        assert resp.status_code == 401

    def test_returns_501_on_programming_error(self, client: TestClient) -> None:
        with patch("modulo.api.routes.admin_housekeeping.set_rls_org"):
            session = _make_mock_session()

            def raise_programming_error(*_args, **_kwargs):  # type: ignore[no-untyped-def]
                raise ProgrammingError("stmt", {}, Exception("missing table"))

            session.begin = MagicMock(return_value=_make_begin_nested(raise_on_enter=True))
            session.begin_nested = MagicMock(side_effect=raise_programming_error)

            async def override_session() -> AsyncGenerator[AsyncMock, None]:
                yield session

            client.app.dependency_overrides[get_db_session] = override_session
            resp = client.post(
                self.URL,
                json={"items": [{"id": str(uuid.uuid4()), "entity_type": "secret"}]},
            )

        assert resp.status_code == 501

    def test_returns_500_on_unexpected_error(self, client: TestClient) -> None:
        with patch("modulo.api.routes.admin_housekeeping.set_rls_org"):
            session = _make_mock_session()
            session.begin = MagicMock(side_effect=RuntimeError("boom"))

            async def override_session() -> AsyncGenerator[AsyncMock, None]:
                yield session

            client.app.dependency_overrides[get_db_session] = override_session
            resp = client.post(
                self.URL,
                json={"items": [{"id": str(uuid.uuid4()), "entity_type": "secret"}]},
            )

        assert resp.status_code == 500

    def test_cleanup_response_model_serializes(self) -> None:
        resp = CleanupResponse(deleted_count=2, errors=[{"id": "x", "entity_type": "secret", "error": "FK"}])
        assert resp.deleted_count == 2
        assert len(resp.errors) == 1


class TestPurgeCheckpoints:
    URL = "/api/v1/admin/housekeeping/checkpoints/purge"

    def test_purges_checkpoints_and_reports_bytes(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.admin_housekeeping.purge_terminal_checkpoints",
                new=AsyncMock(return_value={"checkpoints_purged": 12, "threads_purged": 3, "bytes_freed": 4096}),
            ),
            patch("modulo.api.routes.admin_housekeeping.set_rls_org"),
        ):
            resp = client.post(self.URL, json={"max_age_days": 5, "confirm": True})

        assert resp.status_code == 200
        body = resp.json()
        assert body["checkpoints_purged"] == 12
        assert body["threads_purged"] == 3
        assert body["bytes_freed"] == 4096

    def test_scopes_purge_to_caller_org_with_requested_age(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.admin_housekeeping.purge_terminal_checkpoints",
                new=AsyncMock(return_value={"checkpoints_purged": 0, "threads_purged": 0, "bytes_freed": 0}),
            ) as purge_mock,
            patch("modulo.api.routes.admin_housekeeping.set_rls_org"),
        ):
            client.post(self.URL, json={"max_age_days": 7, "confirm": True})

        purge_mock.assert_awaited_once()
        args = purge_mock.await_args
        assert args is not None
        assert args.kwargs["org_id"] == _ORG_ID
        assert args.kwargs["max_age_days"] == 7

    def test_requires_confirm(self, client: TestClient) -> None:
        with patch("modulo.api.routes.admin_housekeeping.set_rls_org"):
            resp = client.post(self.URL, json={"max_age_days": 5})
        assert resp.status_code == 400

    def test_returns_403_for_non_admin(self, viewer_client: TestClient) -> None:
        resp = viewer_client.post(self.URL, json={"max_age_days": 5, "confirm": True})
        assert resp.status_code == 403

    def test_returns_401_when_unauthenticated(self, unauth_client: TestClient) -> None:
        resp = unauth_client.post(self.URL, json={"max_age_days": 5, "confirm": True})
        assert resp.status_code == 401

    def test_returns_501_on_programming_error(self, client: TestClient) -> None:
        with (
            patch(
                "modulo.api.routes.admin_housekeeping.purge_terminal_checkpoints",
                new=AsyncMock(side_effect=ProgrammingError("stmt", {}, Exception("missing table"))),
            ),
            patch("modulo.api.routes.admin_housekeeping.set_rls_org"),
        ):
            resp = client.post(self.URL, json={"max_age_days": 5, "confirm": True})
        assert resp.status_code == 501


def _make_begin_nested(*, raise_on_enter: bool = False) -> MagicMock:
    cm = MagicMock()
    if raise_on_enter:
        cm.__aenter__ = MagicMock(side_effect=ProgrammingError("stmt", {}, Exception("missing table")))
    else:
        cm.__aenter__ = AsyncMock(return_value=None)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm
